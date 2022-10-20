from LSP.plugin import AbstractPlugin
from LSP.plugin import ClientConfig
from LSP.plugin import css
from LSP.plugin import LspTextCommand
from LSP.plugin import LspWindowCommand
from LSP.plugin import Notification
from LSP.plugin import Request
from LSP.plugin import Response
from LSP.plugin import WorkspaceFolder
from LSP.plugin import register_plugin, unregister_plugin
from LSP.plugin.core.protocol import Location, Point, Position, Range, TextDocumentIdentifier, URI
from LSP.plugin.core.typing import Any, Dict, List, NotRequired, Optional, TypedDict, Union
from LSP.plugin.core.url import parse_uri
from LSP.plugin.core.views import point_to_offset
from LSP.plugin.core.views import range_to_region
from LSP.plugin.core.views import text_document_position_params
from LSP.plugin.core.views import uri_from_view
from collections import deque
from functools import partial
from sublime_lib import ResourcePath
from urllib.parse import parse_qs, urldefrag
import html
import importlib
import json
import mdpopups
import os
import re
import shutil
import sublime
import sublime_plugin
import subprocess
import traceback


# https://github.com/julia-vscode/julia-vscode/blob/main/src/interactive/misc.ts
VersionedTextDocumentPositionParams = TypedDict('VersionedTextDocumentPositionParams', {
    'textDocument': TextDocumentIdentifier,
    'version': int,
    'position': Position
})

# https://github.com/julia-vscode/julia-vscode/blob/main/scripts/packages/VSCodeTestServer/src/testserver_protocol.jl
TestserverRunTestitemRequestParams = TypedDict('TestserverRunTestitemRequestParams', {
    'uri': str,
    'name': str,
    'packageName': str,
    'useDefaultUsings': bool,
    'line': int,
    'column': int,
    'code': str
})

TestMessage = TypedDict('TestMessage', {
    'message': str,
    'location': Optional[Location]
})

TestserverRunTestitemRequestParamsReturn = TypedDict('TestserverRunTestitemRequestParamsReturn', {
    'status': str,
    'message': Optional[List[TestMessage]],
    'duration': Optional[float]
})

# https://github.com/julia-vscode/julia-vscode/blob/main/src/testing/testFeature.ts
TestItemDetail = TypedDict('TestItemDetail', {
    'id': str,
    'label': str,
    'range': Range,
    'code': NotRequired[str],
    'code_range': NotRequired[Range],
    'option_default_imports': NotRequired[bool],
    'option_tags': NotRequired[List[str]],
    'error': NotRequired[str],
    'status': NotRequired[str],  # custom property from LSP-julia to store the testitem status
    'result': NotRequired[TestserverRunTestitemRequestParamsReturn]  # custom property from LSP-julia to store the testitem result
})

PublishTestItemsParams = TypedDict('PublishTestItemsParams', {
    'uri': URI,
    'version': int,
    'project_path': str,
    'package_path': str,
    'package_name': str,
    'testitemdetails': List[TestItemDetail]
})


SETTINGS_FILE = "LSP-julia.sublime-settings"
SESSION_NAME = "julia"
STATUS_BAR_KEY = "lsp_julia_environment"
JULIA_REPL_NAME = "Julia REPL"
JULIA_REPL_TAG = "julia_repl"
CELL_DELIMITERS = ("##", r"#%%", r"# %%")

SUBLIME_VERSION = int(sublime.version())  # This API function is allowed to be invoked at importing time

TESTITEM_ICONS = {
    'testitem': 'Packages/LSP-julia/icons/testitem.png',
    'invalid': '',
    'running': 'Packages/LSP-julia/icons/hourglass.png',
    'passed': 'Packages/LSP-julia/icons/success.png',
    'failed': 'Packages/LSP-julia/icons/failure.png',
    'errored': 'Packages/LSP/icons/error.png'
}

TESTITEM_SCOPES = {
    'testitem': 'region.cyanish markup.testitem.lsp',
    'invalid': 'region.redish markup.error',  # unused (no icon)
    'running': 'region.yellowish markup.testitem.running.lsp',
    'passed': 'region.greenish markup.testitem.passed.lsp',
    'failed': 'region.redish markup.error.testitem.lsp',
    'errored': 'region.redish markup.error.testitem.lsp'
}


def find_output_view(window: sublime.Window, name: str) -> Optional[sublime.View]:
    for view in window.views():
        if view.name() == name:
            return view
    return None


def start_julia_repl(window: sublime.Window, focus: bool, panel: bool) -> None:
    """
    Start Julia REPL in panel via Terminus package.
    """
    settings = sublime.load_settings(SETTINGS_FILE)
    julia_exe = settings.get("julia_executable_path") or "julia"
    cmd = [julia_exe, "--banner=no", "--project"]  # start in current project environment if available
    window.run_command("terminus_open", {
        "cmd": cmd,
        "cwd": "${file_path:${folder}}",
        "title": JULIA_REPL_NAME,
        "panel_name": JULIA_REPL_NAME,
        "show_in_panel": panel,
        "focus": focus,
        "tag": JULIA_REPL_TAG,
        "env": settings.get("repl_env_variables"),
    })


def ensure_julia_repl(window: sublime.Window) -> bool:
    """
    Start Julia REPL in panel via Terminus package if not already running.
    """
    if not window.find_output_panel(JULIA_REPL_NAME) and not find_output_view(window, JULIA_REPL_NAME):
        start_julia_repl(window, False, True)
        return False
    return True


def send_julia_repl(window: sublime.Window, code_block: str) -> None:
    """
    Send a code block string to Julia REPL via Terminus package.
    """

    # ensure code block ends with newline to enforce execution in REPL
    if not code_block.endswith("\n"):
        code_block += "\n"
    window.run_command("terminus_send_string", {"string": code_block, "tag": JULIA_REPL_TAG})


def versioned_text_document_position_params(view: sublime.View, location: int) -> VersionedTextDocumentPositionParams:
    """
    Custom Julia-specific extension to the LSP.

    https://github.com/julia-vscode/LanguageServer.jl/blob/master/src/extensions/extensions.jl
    """
    position_params = text_document_position_params(view, location)
    return {
        "textDocument": position_params["textDocument"],
        "position": position_params["position"],
        "version": view.change_count()
    }


def is_julia_environment(folder_path: str) -> bool:
    """
    Check whether a given folder path is a valid Julia project environment, i.e. it contains a file Project.toml
    or JuliaProject.toml.
    """
    return os.path.isfile(os.path.join(folder_path, "Project.toml")) or \
        os.path.isfile(os.path.join(folder_path, "JuliaProject.toml"))


def find_julia_environment(folder_path: str) -> Optional[str]:
    """
    Search through parent directories for a Julia project environment.
    """
    while os.path.basename(folder_path):
        if is_julia_environment(folder_path):
            return folder_path
        else:
            folder_path = os.path.dirname(folder_path)
    return None


def prepare_markdown(content: str) -> str:
    """
    This function applies a few modifications to the Markdown content used in hover popups and the documentation sheet,
    in order to workaround some parsing inconsistencies in mdpopups' Markdown-to-minihtml converter and to enable links
    with references to a documentation page.
    """

    # Workaround CommonMark deficiency: two spaces followed by a newline should result in a new paragraph
    content = re.sub("(\\S)  \n", "\\1\n\n", content)
    # Add another newline before horizontal rule
    content = re.sub("\n---", "\n\n---", content)
    # Add another newline before list items
    content = re.sub("\n- ", "\n\n- ", content)
    # Replace [`title`](@ref) links with the corresponding command to navigate the documentation with a new search query
    content = re.sub(
        r"\[`(.+?)`\]\(@ref.*?\)", r"""<a href='subl:julia_search_documentation {"word": "\1"}'>`\1`</a>""", content)
    # Remove parameters after fenced code block language identifier
    content = re.sub("```jldoctest;.*?\n", "```jldoctest\n", content)
    return content


def startupinfo():
    if sublime.platform() == "windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 11
        return startupinfo
    return None


class JuliaLanguageServer(AbstractPlugin):

    testitems = {}  # type: Dict[URI, PublishTestItemsParams]
    testitem_in_progress = False
    testitem_error_regions = {}  # type: Dict[URI, List[sublime.Region]]
    testitem_error_annotations = {}  # type: Dict[URI, List[str]]

    def __init__(self, weaksession) -> None:
        super().__init__(weaksession)
        if sublime.load_settings(SETTINGS_FILE).get("show_environment_status"):
            session = self.weaksession()
            if session:
                env_name = os.path.basename(session.working_directory) if session.working_directory else \
                    JuliaLanguageServer.default_julia_environment()
                session.set_window_status_async(STATUS_BAR_KEY, "Julia env: {}".format(env_name))

    @classmethod
    def name(cls) -> str:
        return SESSION_NAME

    @classmethod
    def additional_variables(cls) -> Optional[Dict[str, str]]:
        variables = dict()
        variables["julia_exe"] = cls.julia_exe()
        variables["server_path"] = cls.basedir()
        return variables

    @classmethod
    def basedir(cls) -> str:
        return os.path.join(cls.storage_path(), "LSP-julia", "languageserver")

    @classmethod
    def testrunnerdir(cls) -> str:
        return os.path.join(cls.storage_path(), "LSP-julia", "testrunner")

    @classmethod
    def version_file(cls) -> str:
        return os.path.join(cls.basedir(), "VERSION")

    @classmethod
    def packagedir(cls) -> str:
        return os.path.join(sublime.packages_path(), "LSP-julia")

    @classmethod
    def julia_exe(cls) -> str:
        return str(sublime.load_settings(SETTINGS_FILE).get("julia_executable_path")) or "julia"

    @classmethod
    def julia_version(cls) -> str:
        return subprocess.check_output(
            [cls.julia_exe(), "--version"], startupinfo=startupinfo()).decode("utf-8").rstrip().split()[-1]

    @classmethod
    def default_julia_environment(cls) -> str:
        major, minor, _ = cls.julia_version().split(".")
        return "v{}.{}".format(major, minor)

    @classmethod
    def server_version(cls) -> str:
        return "781240a"  # LanguageServer v4.3.2-DEV

    @classmethod
    def needs_update_or_installation(cls) -> bool:
        if not shutil.which(cls.julia_exe()):
            msg = "The executable \"{}\" could not be found. Set up the path to the Julia executable by running the command\n\n\tPreferences: LSP-julia Settings\n\nfrom the command palette.".format(cls.julia_exe())
            raise RuntimeError(msg)
        try:
            with open(cls.version_file(), "r") as fp:
                return cls.server_version() != fp.read().strip()
        except OSError:
            return True

    @classmethod
    def install_or_update(cls) -> None:
        shutil.rmtree(cls.basedir(), ignore_errors=True)
        try:
            os.makedirs(cls.basedir(), exist_ok=True)
            for file in ("Project.toml", "Manifest.toml"):
                ResourcePath.from_file_path(
                    os.path.join(cls.packagedir(), "server", file)).copy(os.path.join(cls.basedir(), file))
            # TODO Use cls.basedir() as DEPOT_PATH for language server
            os.makedirs(cls.testrunnerdir(), exist_ok=True)
            for file in ("Project.toml", "runtestitem.jl"):
                ResourcePath.from_file_path(
                    os.path.join(cls.packagedir(), "testrunner", file)).copy(os.path.join(cls.testrunnerdir(), file))
            returncode = subprocess.call([
                cls.julia_exe(),
                "--startup-file=no",
                "--history-file=no",
                "--project={}".format(cls.basedir()),
                "--eval", "ENV[\"JULIA_SSL_CA_ROOTS_PATH\"] = \"\"; import Pkg; Pkg.instantiate()"
            ])
            if returncode == 0:
                with open(cls.version_file(), "w") as fp:
                    fp.write(cls.server_version())
            else:
                error_msg = "An error occured while trying to install the Language Server. Check the console for possible error messages or consider to open an issue in the LSP-julia issue tracker on GitHub."
                sublime.error_message(error_msg)
        except Exception:
            shutil.rmtree(cls.basedir(), ignore_errors=True)
            raise

    @classmethod
    def on_pre_start(
        cls,
        window: sublime.Window,
        initiating_view: sublime.View,
        workspace_folders: List[WorkspaceFolder],
        configuration: ClientConfig
    ) -> Optional[str]:
        # The working directory is used by the language server to find the Julia project environment, if not explicitly
        # given as a parameter of runserver() or as a command line argument. We can make use of this to avoid adjusting
        # the "command" setting everytime with a new environment argument when the server starts.
        # If one or more folders are opened in Sublime Text, and one of it contains the initiating view, that folder is
        # used as the working directory. This avoids to accidentally use a nested environment, e.g. if the initiating
        # view is a file from a `docs` or `test` subdirectory.
        # Otherwise we search through parent directories of the initiating view for a Julia environment. If no
        # environment is found, the language server will fall back to the default Julia environment.
        view_uri = uri_from_view(initiating_view)
        for folder in workspace_folders:
            if folder.includes_uri(view_uri):
                return folder.path
        file_path = initiating_view.file_name()
        if file_path:
            return find_julia_environment(os.path.dirname(file_path))
        return None

    def on_server_response_async(self, method: str, response: Response) -> None:
        if method == "textDocument/hover":
            if response.result and isinstance(response.result["contents"], dict) and response.result["contents"].get("kind") == "markdown":  # pyright: ignore
                response.result["contents"]["value"] = prepare_markdown(response.result["contents"]["value"])  # pyright: ignore

    def _render_testitems(self, uri: URI, new_result_idx: Optional[int] = None) -> None:
        params = self.testitems.get(uri)
        if not params:
            return
        scheme, path = parse_uri(uri)
        if scheme != 'file':
            return
        session = self.weaksession()
        if not session:
            return
        view = session.window.find_open_file(path)
        if not view:
            return

        version = params['version']

        regions = {
            'testitem': [],
            'invalid': [],
            'running': [],
            'passed': [],
            'failed': [],
            'errored': []
        }  # type: Dict[str, List[sublime.Region]]

        annotations = {
            'testitem': [],
            'invalid': [],
            'running': [],
            'passed': [],
            'failed': [],
            'errored': []
        }  # type: Dict[str, List[str]]

        for idx, item in enumerate(params['testitemdetails']):
            testitem_region = sublime.Region(point_to_offset(Point.from_lsp(item['range']['start']), view))
            run_test_annotation = '<a href="{}#idx={}&amp;version={}">Run Test</a>'.format(html.escape(uri), idx, version)
            duration = item.get('result', {}).get('duration')
            if duration is not None:
                if duration < 100:
                    run_test_annotation += " ({}ms)".format(round(duration))
                else:
                    run_test_annotation += " ({:0.2f}s)".format(duration/1000)
            status = item.get('status', 'testitem')
            error = item.get('error')
            if error and isinstance(error, str):
                regions['invalid'].append(testitem_region)
                annotations['invalid'].append(error)
            elif status == 'testitem':
                regions[status].append(testitem_region)
                annotations[status].append(run_test_annotation)
            elif status == 'running':
                regions[status].append(testitem_region)
                annotations[status].append('Running…')
            elif status == 'passed':
                regions[status].append(testitem_region)
                annotations[status].append(run_test_annotation)
            elif status == 'failed':
                regions[status].append(testitem_region)
                annotations[status].append(run_test_annotation)
                if idx == new_result_idx:
                    for message in item['result']['message']:  # pyright: ignore
                        location = message['location']
                        if location:
                            self.testitem_error_regions.setdefault(uri, []).append(range_to_region(location['range'], view))
                            self.testitem_error_annotations.setdefault(uri, []).append(message['message'])
            elif status == 'errored':
                regions[status].append(testitem_region)
                annotations[status].append(run_test_annotation)
                if idx == new_result_idx:
                    self.testitem_error_regions.setdefault(uri, []).append(range_to_region(item['result']['message'][0]['location']['range'], view))  # pyright: ignore
                    self.testitem_error_annotations.setdefault(uri, []).append(item['result']['message'][0]['message'])  # pyright: ignore

        testitem_annotation_color = view.style_for_scope('region.cyanish markup.accent.testitem.lsp')['foreground']
        error_annotation_color = view.style_for_scope('region.redish markup.error.testitem.lsp')['foreground']

        for status in ('testitem', 'invalid', 'running', 'passed', 'failed', 'errored'):
            regions_key = 'lsp_julia_testitem_{}'.format(status)
            if regions[status]:
                view.add_regions(
                    regions_key,
                    regions[status],
                    scope=TESTITEM_SCOPES[status],
                    icon=TESTITEM_ICONS[status],
                    flags=sublime.HIDE_ON_MINIMAP | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
                    annotations=annotations[status],
                    annotation_color=error_annotation_color if status == 'invalid' else testitem_annotation_color,
                    on_navigate=self._run_testitem if status in ('testitem', 'passed', 'failed', 'errored') else None)
            else:
                view.erase_regions(regions_key)

    def _render_error_locations(self, uri: URI) -> None:
        scheme, path = parse_uri(uri)
        if scheme != 'file':
            return
        session = self.weaksession()
        if not session:
            return
        view = session.window.find_open_file(path)
        if not view:
            return
        regions_key = 'lsp_julia_testitem_error_location'
        regions = self.testitem_error_regions.get(uri)
        if regions:
            error_annotation_color = view.style_for_scope('region.redish markup.error.testitem.lsp')['foreground']
            view.add_regions(
                regions_key,
                regions,
                flags=sublime.HIDE_ON_MINIMAP | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
                annotations=self.testitem_error_annotations[uri],
                annotation_color=error_annotation_color,
                on_close=lambda: self._hide_annotations(uri))
        else:
            view.erase_regions(regions_key)

    def _hide_annotations(self, uri: URI) -> None:
        # TODO this will close all annotations if the close button of any of them is clicked.
        # We probably need to use a unique region key per annotation in order to make it possible
        # to close annotations one by one.
        self.testitem_error_regions[uri] = []
        self.testitem_error_annotations[uri] = []
        self._render_error_locations(uri)

    def _run_testitem(self, href: str) -> None:
        if self.testitem_in_progress:
            sublime.active_window().status_message("Another testitem is already running")
            return
        self.testitem_in_progress = True
        uri, fragment = urldefrag(href)
        pq = parse_qs(fragment)
        idx = int(pq['idx'][0])
        version = int(pq['version'][0])

        testitemparams = self.testitems.get(uri)
        if not testitemparams:
            return
        if version != testitemparams['version']:
            return
        self.testitems[uri]['testitemdetails'][idx]['status'] = 'running'
        self.testitem_error_regions[uri] = []
        self.testitem_error_annotations[uri] = []
        self._render_testitems(uri)
        self._render_error_locations(uri)

        def run_async(
            uri: URI,
            idx: int,
            version: int,
            params: TestserverRunTestitemRequestParams,
            project_path: str,
            package_path: str,
            package_name: str
        ) -> None:
            try:
                params_extended = {
                    'uri': params['uri'],
                    'name': params['name'],
                    'packageName': params['packageName'],
                    'useDefaultUsings': params['useDefaultUsings'],
                    'line': params['line'],
                    'column': params['column'],
                    'code': params['code'],
                    'project_path': project_path,
                    'package_path': package_path,
                    'package_name': package_name
                }
                params_json = json.dumps(params_extended, separators=(',', ':'))
                # TODO run process in a non-blocking way!
                result_json = subprocess.check_output([
                    JuliaLanguageServer.julia_exe(),
                    "--startup-file=no",
                    "--history-file=no",
                    "--project={}".format(JuliaLanguageServer.testrunnerdir()),
                    "runtestitem.jl",
                    params_json
                ], cwd=JuliaLanguageServer.testrunnerdir(), startupinfo=startupinfo()).decode("utf-8")
                result = json.loads(result_json)
                sublime.set_timeout(lambda: self._on_testitem_result(uri, idx, version, result))
            except Exception:
                self.testitem_in_progress = False
                self.testitems[uri]['testitemdetails'][idx]['error'] = "An error occured while trying to run this testitem.<br>Please check the console and consider to open an issue report in the LSP-julia GitHub repo."
                traceback.print_exc()
                sublime.set_timeout(lambda: self._render_testitems(uri))

        testitem = testitemparams['testitemdetails'][idx]
        assert testitem.get('error') is None
        code_range = testitem.get('code_range')
        if not code_range:
            return
        code = testitem.get('code')
        if not code:
            return
        params = {
            'uri': uri,
            'name': testitem['label'],
            'packageName': testitemparams['package_name'],
            'useDefaultUsings': True,
            'line': code_range['start']['line'],
            'column': code_range['start']['character'],
            'code': code
        }  # type: TestserverRunTestitemRequestParams
        sublime.set_timeout_async(partial(run_async, uri, idx, version, params, testitemparams['project_path'], testitemparams['package_path'], testitemparams['package_name']))

    def _on_testitem_result(
        self,
        uri: URI,
        idx: int,
        version: int,
        result: TestserverRunTestitemRequestParamsReturn
    ) -> None:
        self.testitem_in_progress = False
        params = self.testitems.get(uri)
        if not params:
            return
        # reject result if the language server has updated the testitems in the file
        # (i.e. if there were changes in the meantime)
        if version != params['version']:
            return
        self.testitems[uri]['testitemdetails'][idx]['status'] = result['status']
        self.testitems[uri]['testitemdetails'][idx]['result'] = result
        self._render_testitems(uri, idx)
        self._render_error_locations(uri)

    def m_julia_publishTestitems(self, params: PublishTestItemsParams) -> None:
        if params:
            uri = params['uri']
            self.testitems[uri] = params
            self._render_testitems(uri)


def plugin_loaded() -> None:
    register_plugin(JuliaLanguageServer)


def plugin_unloaded() -> None:
    unregister_plugin(JuliaLanguageServer)


class JuliaActivateEnvironmentCommand(LspWindowCommand):
    """
    Can be invoked from the command palette to switch the active Julia project environment.
    The active Julia project environment detemines the Julia packages used by the language server to provide
    autocomplete suggestions and diagnostics.
    """

    session_name = SESSION_NAME

    def run(self, env_path: str) -> None:
        if env_path == "__select_folder_dialog":
            sublime.select_folder_dialog(self.on_select_folder, multi_select=False)  # pyright: ignore  # compatible types for self.on_select_folder are ensured due to multi_select=False
        else:
            self.activate_environment(env_path)

    def on_select_folder(self, folder_path: Optional[str]) -> None:
        if folder_path:
            if is_julia_environment(folder_path):
                self.activate_environment(folder_path)
            else:
                self.window.status_message("The selected folder is not a valid Julia environment")

    def activate_environment(self, env_path: str) -> None:
        session = self.session()
        if not session:
            return
        session.send_notification(Notification("julia/activateenvironment", {"envPath": env_path}))
        if sublime.load_settings(SETTINGS_FILE).get("show_environment_status"):
            env_name = os.path.basename(env_path)
            session.set_window_status_async(STATUS_BAR_KEY, "Julia env: {}".format(env_name))

    def input(self, args: dict) -> Optional[sublime_plugin.ListInputHandler]:
        if "env_path" not in args:
            return EnvPathInputHandler(self.session().get_workspace_folders())  # pyright: ignore [reportOptionalMemberAccess]


class EnvPathInputHandler(sublime_plugin.ListInputHandler):
    """
    Used by JuliaActivateEnvironmentCommand to display the available Julia project environments the user can choose from.
    """

    KIND_DEFAULT_ENVIRONMENT = (sublime.KIND_ID_COLOR_YELLOWISH, "d", "default environment")
    KIND_WORKSPACE_FOLDER = (sublime.KIND_ID_COLOR_PURPLISH, "f", "workspace folder")

    def __init__(self, workspace_folders: List[WorkspaceFolder]) -> None:
        self.workspace_folders = workspace_folders

    def list_items(self) -> List[sublime.ListInputItem]:
        # add default Julia environments from .julia/environments
        julia_env_home = os.path.expanduser(os.path.join("~", ".julia", "environments"))
        names = [env for env in reversed(os.listdir(julia_env_home)) if os.path.isdir(os.path.join(julia_env_home, env))]  # collect all folder names in .julia/environments
        paths = [os.path.join(julia_env_home, env) for env in names]  # the corresponding folder paths
        items = [sublime.ListInputItem(name, path, kind=self.KIND_DEFAULT_ENVIRONMENT) for name, path in zip(names, paths)]
        # add workspace folders on top of the list if they are valid Julia project environments
        for workspace_folder in reversed(self.workspace_folders):
            if workspace_folder.path not in paths and is_julia_environment(workspace_folder.path):
                items.insert(0, sublime.ListInputItem(workspace_folder.name, workspace_folder.path, kind=self.KIND_WORKSPACE_FOLDER))
        # add option for folder picker dialog
        items.insert(0, sublime.ListInputItem("(pick a folder…)", "__select_folder_dialog"))
        return items

    def placeholder(self) -> str:
        return "Select Julia project/environment folder"

    def preview(self, value: Optional[str]) -> Union[sublime.Html, str, None]:
        if value == "__select_folder_dialog":
            return "Open a folder picker dialog to select a Julia project"
        else:
            return sublime.Html("<i>{}</i>".format(value)) if value else None

    def validate(self, value) -> bool:
        return value is not None


class JuliaOpenReplCommand(sublime_plugin.WindowCommand):
    """
    Start a Julia REPL via the Terminus package, or focus panel if already started.
    """

    def is_enabled(self) -> bool:
        return importlib.find_loader("Terminus") is not None

    def run(self, panel: bool = True) -> None:
        repl_view = find_output_view(self.window, JULIA_REPL_NAME)
        repl_panel = self.window.find_output_panel(JULIA_REPL_NAME)

        if repl_view:
            self.window.focus_view(repl_view)
        elif repl_panel:
            self.window.run_command("show_panel", {"panel": "output.{}".format(JULIA_REPL_NAME)})
            self.window.focus_view(repl_panel)
        else:
            start_julia_repl(self.window, True, panel)


class JuliaSelectCodeBlockCommand(LspTextCommand):
    """
    Can be invoked to select the code block containing the current cursor position.
    Maybe not very useful on its own, but rather when combined with running the code in the Julia REPL.
    """

    session_name = SESSION_NAME

    def run(self, edit: sublime.Edit) -> None:
        params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
        self.session_by_name(self.session_name).send_request(Request("julia/getCurrentBlockRange", params), self.on_result)  # pyright: ignore [reportOptionalMemberAccess]

    def on_result(self, params: Any) -> None:
        a = point_to_offset(Point.from_lsp(params[0]), self.view)
        b = point_to_offset(Point.from_lsp(params[1]), self.view)
        self.view.run_command("lsp_selection_set", {"regions": [(a, b)]})


class JuliaRunCodeBlockCommand(LspTextCommand):
    """
    Can be invoked to execute the current selection in the Julia REPL. If no text is selected, get the code block
    containing the current cursor position from the language server and execute it in the Julia REPL.
    """

    session_name = SESSION_NAME

    def is_enabled(self, event: Optional[dict] = None, point: Optional[int] = None) -> bool:
        # Language server must be ready
        if not super().is_enabled(event, point):
            return False
        # Terminus package must be installed
        if not importlib.find_loader("Terminus"):
            return False
        # cursor must not be at end of file
        if self.view.sel()[0].b == self.view.size():
            return False
        return True

    def run(self, edit: sublime.Edit, event: Optional[dict] = None, point: Optional[int] = None) -> None:
        window = self.view.window()
        if not window:
            return
        # ensure that Terminus output panel for Julia REPL is available
        repl_ready = ensure_julia_repl(window)
        sel = self.view.sel()[0]
        if sel.empty():
            params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
            self.session_by_name(self.session_name).send_request(Request("julia/getCurrentBlockRange", params), self.on_result)  # pyright: ignore [reportOptionalMemberAccess]
        else:
            code_block = self.view.substr(sel)
            if repl_ready:
                send_julia_repl(window, code_block)
            else:
                # give Terminus a bit time to initialize, otherwise the terminus_send_string command doesn't work
                sublime.set_timeout(lambda: send_julia_repl(window, code_block), 5)

    def on_result(self, params: Any) -> None:
        window = self.view.window()
        if not window:
            return
        a = point_to_offset(Point.from_lsp(params[0]), self.view)
        b = point_to_offset(Point.from_lsp(params[1]), self.view)
        c = point_to_offset(Point.from_lsp(params[2]), self.view)
        code_block = self.view.substr(sublime.Region(a, b))
        self.view.run_command("lsp_selection_set", {"regions": [(c, c)]})  # move cursor to next code block
        self.view.show_at_center(c)
        send_julia_repl(window, code_block)


class JuliaRunCodeCellCommand(sublime_plugin.TextCommand):
    """
    Can be invoked to execute the current selection, or if no text is selected, the code cell containing the current
    cursor position in the Julia REPL. Code cells are delimited by specially formatted comments.
    """

    def is_enabled(self) -> bool:
        # must be Julia file
        if not self.view.match_selector(0, "source.julia"):
            return False
        # Terminus package must be installed
        if not importlib.find_loader("Terminus"):
            return False
        # cursor must not be at end of file
        if self.view.sel()[0].b == self.view.size():
            return False
        return True

    def run(self, edit: sublime.Edit) -> None:
        window = self.view.window()
        if not window:
            return
        sel = self.view.sel()[0]
        repl_ready = ensure_julia_repl(window)
        if sel.empty():
            line_count = self.view.rowcol(self.view.size())[0]
            # get start and end line of code cell
            line_start = self.view.rowcol(sel.b)[0]
            line_end = line_start
            while line_start >= 0:
                point = self.view.text_point(line_start, 0)
                if self.view.substr(self.view.line(point)).startswith(CELL_DELIMITERS):
                    break
                line_start -= 1
            line_start += 1
            while line_end <= line_count:
                point = self.view.text_point(line_end, 0)
                if self.view.substr(self.view.line(point)).startswith(CELL_DELIMITERS):
                    break
                line_end += 1
            code_block = ""
            for line in range(line_start, line_end):
                code_line = self.view.substr(self.view.line(self.view.text_point(line, 0)))
                # remove empty and comment lines
                if code_line and not code_line.lstrip().startswith("#"):
                    code_block += code_line + "\n"
            # move cursor and scroll to next cell
            next_cell = line_end + 1
            while next_cell < line_count:
                if not self.view.substr(self.view.line(self.view.text_point(next_cell, 0))).startswith("#"):
                    break
                next_cell += 1
            c = self.view.text_point(next_cell, 0)
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(c, c))
            self.view.show_at_center(c)
        else:
            code_block = self.view.substr(sel)
        if repl_ready:
            send_julia_repl(window, code_block)
        else:
            sublime.set_timeout(lambda: send_julia_repl(window, code_block), 5)


class JuliaSearchDocumentationCommand(LspWindowCommand):
    """
    Can be invoked to search the Julia documentation.
    """

    session_name = SESSION_NAME

    _sheet_id = None  # type: Optional[int]

    _last_words = deque(maxlen=100)
    _next_words = deque()
    _current_word = None  # type: Optional[str]

    def run(self, word: str) -> None:
        if word == "__back":
            try:
                word = self._last_words.pop()
                self._next_words.append(self._current_word)
                self._current_word = word
            except IndexError:
                return
        elif word == "__forward":
            try:
                word = self._next_words.pop()
                self._last_words.append(self._current_word)
                self._current_word = word
            except IndexError:
                return
        else:
            # Only update the word history if the new query is different from the last one. Otherwise it would be bad UX
            # when you click on the "Back" or "Forward" buttons and nothing seems to happen. A new request to the server
            # and content update of the HtmlSheet is still required, because the documentation sheet might have been
            # closed in the meantime.
            if self._current_word is not None and self._current_word != word:
                self._last_words.append(self._current_word)
                self._next_words.clear()
            self._current_word = word

        self.session().send_request(Request("julia/getDocFromWord", {"word": word}), self.on_result)  # pyright: ignore [reportOptionalMemberAccess]

    def on_result(self, response: str) -> None:
        selected_sheets = self.window.selected_sheets()
        # There is no way to find a particular HtmlSheet, other than by storing its id.
        # See https://github.com/sublimehq/sublime_text/issues/3826
        for sheet in self.window.sheets():
            if isinstance(sheet, sublime.HtmlSheet) and sheet.id() == self._sheet_id:
                if sheet not in selected_sheets:
                    selected_sheets.append(sheet)
                    self.window.select_sheets(selected_sheets)
                break
        else:
            active_view = self.window.active_view()
            # If there is not yet a sheet for the Julia documentation, open it in side-by-side mode
            if SUBLIME_VERSION >= 4135:
                sheet = self.window.new_html_sheet("Julia Documentation", "", flags=sublime.ADD_TO_SELECTION)
            else:
                sheet = self.window.new_html_sheet("Julia Documentation", "")
                # Workaround for https://github.com/sublimehq/sublime_text/issues/5488
                selected_sheets.append(sheet)
                self.window.select_sheets(selected_sheets)
            self._sheet_id = sheet.id()
            if active_view and active_view.is_valid():
                self.window.focus_view(active_view)

        frontmatter = mdpopups.format_frontmatter({
            "allow_code_wrap": True,
            "language_map": {
                "julia": (("julia", "julia-repl", "jldoctest"), ("Julia/Julia",))
            },
            "markdown_extensions": [
                "markdown.extensions.admonition",
                "markdown.extensions.attr_list",
                "markdown.extensions.nl2br",
                {
                    "pymdownx.escapeall": {
                        "hardbreak": True,
                        "nbsp": False
                    }
                },
                {
                    "pymdownx.magiclink": {
                        "hide_protocol": True,
                        "repo_url_shortener": True
                    }
                }
            ]
        })

        # Add navigation toolbar with "Back", "Forward" and "Search" links
        toolbar_links = []  # type: List[str]
        toolbar_links.append("""<a title='Go back one page' href='subl:julia_search_documentation {"word": "__back"}'>Back</a>""" if len(self._last_words) else "Back")
        toolbar_links.append("""<a title='Go forward one page' href='subl:julia_search_documentation {"word": "__forward"}'>Forward</a>""" if len(self._next_words) else "Forward")
        toolbar_links.append("""<a href='subl:julia_search_documentation'>Search</a>""")
        toolbar = "<div class='toolbar'>" + " | ".join(toolbar_links) + "</div><hr>\n"

        markdown_content = prepare_markdown(response)

        # Replace Markdown links with `file:` URI with actual HTML links and `subl:open_file` command, because the
        # `file:` protocol is not supported for links in minihtml and there is no way to utilize a callback function for
        # links in a HtmlSheet
        if SUBLIME_VERSION >= 4127:
            # The `encoded_position` argument for the open_file command was introduced in ST 4127
            # https://github.com/sublimehq/sublime_text/issues/4800
            markdown_content = re.sub(
                r"\[(.+?:\d+)\]\(file:///.+?#\d+\)",
                r"""<a href='subl:open_file {"file": "\1", "encoded_position": true}'>\1</a>""",
                markdown_content)
        else:
            markdown_content = re.sub(
                r"\[(.+?)(:\d+)\]\(file:///.+?#\d+\)",
                r"""<a href='subl:open_file {"file": "\1"}'>\1\2</a>""",
                markdown_content)

        content = frontmatter + toolbar + markdown_content

        mdpopups.update_html_sheet(sheet, content, css=css().sheets, wrapper_class="lsp_sheet")

    def input(self, args: dict) -> Optional[sublime_plugin.TextInputHandler]:
        if "word" not in args:
            return WordInputHandler()


class WordInputHandler(sublime_plugin.TextInputHandler):
    def placeholder(self) -> str:
        return "Search Julia docs"

    def validate(self, text: str) -> bool:
        return text != ""


class JuliaShowDocumentationCommand(LspTextCommand):
    """
    Can be invoked to search the Julia documentation about the word at the current cursor position
    or from the right-click context menu.
    """

    session_name = SESSION_NAME

    def is_enabled(self, event: Optional[dict] = None, point: Optional[int] = None) -> bool:
        # Language server must be ready
        if not super().is_enabled(event, point):
            return False
        # Cursor position or right click must be on a word
        if event is not None and "x" in event and "y" in event:
            pt = self.view.window_to_text((event["x"], event["y"]))
        elif len(self.view.sel()) != 1:
            return False
        else:
            pt = self.view.sel()[0].b
        # The View.word() API isn't useful to decide whether a point is on a word, it may return
        # strings filled with whitespace or punctuation symbols.
        point_classification = self.view.classify(pt)
        return point_classification == 512 or bool(point_classification & sublime.CLASS_WORD_START) or \
            bool(point_classification & sublime.CLASS_WORD_END)

    def is_visible(self, event: Optional[dict] = None, point: Optional[int] = None) -> bool:
        return self.is_enabled(event, point)

    def run(self, edit: sublime.Edit, event: Optional[dict] = None, point: Optional[int] = None) -> None:
        if event is not None and "x" in event and "y" in event:
            pt = self.view.window_to_text((event["x"], event["y"]))
        else:
            pt = self.view.sel()[0].b
        # we already know that the point is really on a word due to self.is_enabled
        word = self.view.substr(self.view.word(pt))
        window = self.view.window()
        if window:
            window.run_command("julia_search_documentation", {"word": word})
