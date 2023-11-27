from LSP.plugin import AbstractPlugin
from LSP.plugin import ClientConfig
from LSP.plugin import css
from LSP.plugin import LspTextCommand
from LSP.plugin import LspWindowCommand
from LSP.plugin import Notification
from LSP.plugin import parse_uri
from LSP.plugin import Request
from LSP.plugin import Response
from LSP.plugin import WorkspaceFolder
from LSP.plugin import register_plugin, unregister_plugin
from LSP.plugin.core.protocol import DocumentUri, Location, Point, Position, Range, TextDocumentIdentifier
from LSP.plugin.core.typing import Any, Dict, List, NotRequired, Optional, Set, StrEnum, Tuple, TypedDict, Union
from LSP.plugin.core.typing import cast
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
import itertools
import json
import mdpopups
import os
import re
import shutil
import sublime
import sublime_plugin
import subprocess
import threading
import traceback


# https://github.com/julia-vscode/julia-vscode/blob/main/src/interactive/misc.ts
# https://github.com/julia-vscode/LanguageServer.jl/blob/master/src/extensions/extensions.jl
VersionedTextDocumentPositionParams = TypedDict('VersionedTextDocumentPositionParams', {
    'textDocument': TextDocumentIdentifier,
    'version': int,
    'position': Position
})

# https://github.com/julia-vscode/julia-vscode/blob/main/src/testing/testFeature.ts
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

TestItemDetail = TypedDict('TestItemDetail', {
    'id': str,
    'label': str,
    'range': Range,
    'code': Optional[str],
    'code_range': Optional[Range],
    'option_default_imports': Optional[bool],
    'option_tags': Optional[List[str]]
})

TestSetupDetail = TypedDict('TestSetupDetail', {
    'name': str,
    'range': Range,
    'code': Optional[str],
    'code_range': Optional[Range]
})

TestErrorDetail = TypedDict('TestErrorDetail', {
    'range': Range,
    'error': str
})

PublishTestsParams = TypedDict('PublishTestsParams', {
    'uri': DocumentUri,
    'version': NotRequired[int],
    'project_path': str,
    'package_path': str,
    'package_name': str,
    'testitemdetails': List[TestItemDetail],
    'testsetupdetails': List[TestSetupDetail],
    'testerrordetails': List[TestErrorDetail]
})

# Parameters for the runtestitem.jl script, which also requires project_path and package_path
TestserverRunTestitemRequestExtendedParams = TypedDict('TestserverRunTestitemRequestExtendedParams', {
    'uri': str,
    'name': str,
    'packageName': str,
    'useDefaultUsings': bool,
    'line': int,
    'column': int,
    'code': str,
    'project_path': str,
    'package_path': str
})


class TestItemStatus(StrEnum):
    # Used as response values by VSCodeTestServer.jl
    Passed = 'passed'
    Failed = 'failed'
    Errored = 'errored'
    # Additional status indicators used for the different annotations in the view
    Undetermined = 'undetermined'
    Pending = 'pending'
    Invalid = 'invalid'


# sublime.Kind tuples for the "Run Testitem" QuickPanelItems
KIND_PASSED = (sublime.KIND_ID_COLOR_GREENISH, "✓", "Passed")
KIND_FAILED = (sublime.KIND_ID_COLOR_REDISH, "✗", "Failed")
KIND_ERRORED = (sublime.KIND_ID_COLOR_REDISH, "✗", "Errored")

# sublime.Kind tuples for the "Change Current Environment" QuickPanelItems
KIND_DEFAULT_ENVIRONMENT = (sublime.KIND_ID_COLOR_YELLOWISH, "d", "Default Environment")
KIND_WORKSPACE_FOLDER = (sublime.KIND_ID_COLOR_PURPLISH, "f", "Workspace Folder")

# unnamed PointClassification flags for View.classify (no guarantee of correctness)
CLASS_INSIDE_WORD = 512
# CLASS_BRACKET_OPEN = 4096
# CLASS_BRACKET_CLOSE = 8192

TESTITEM_ICONS = {
    TestItemStatus.Passed: 'Packages/LSP-julia/icons/passed.png',
    TestItemStatus.Failed: 'Packages/LSP-julia/icons/failed.png',
    TestItemStatus.Errored: 'Packages/LSP/icons/error.png',
    TestItemStatus.Undetermined: '',
    TestItemStatus.Pending: 'Packages/LSP-julia/icons/stopwatch.png',
    TestItemStatus.Invalid: ''
}  # type: Dict[TestItemStatus, str]

TESTITEM_SCOPES = {
    TestItemStatus.Passed: 'region.greenish markup.testitem.passed.lsp',
    TestItemStatus.Failed: 'region.redish markup.error markup.testitem.failed.lsp',
    TestItemStatus.Errored: 'region.redish markup.error markup.testitem.errored.lsp',
    TestItemStatus.Undetermined: 'region.cyanish markup.testitem.undetermined.lsp',
    TestItemStatus.Pending: 'region.yellowish markup.testitem.pending.lsp',
    TestItemStatus.Invalid: 'region.redish markup.error markup.testitem.invalid.lsp'
}  # type: Dict[TestItemStatus, str]

TESTITEM_KINDS = {
    TestItemStatus.Passed: KIND_PASSED,
    TestItemStatus.Failed: KIND_FAILED,
    TestItemStatus.Errored: KIND_ERRORED
}  # type: Dict[TestItemStatus, Tuple[int, str, str]]

ST_VERSION = int(sublime.version())  # This API function is allowed to be invoked at importing time
SETTINGS_FILE = "LSP-julia.sublime-settings"
SESSION_NAME = "julia"
STATUS_BAR_KEY = "lsp_julia_environment"
JULIA_REPL_NAME = "Julia REPL"
JULIA_REPL_TAG = "julia_repl"
CELL_DELIMITERS = ("##", r"#%%", r"# %%")


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
    return_focus = window.active_view()
    # ensure code block ends with newline to enforce execution in REPL
    if not code_block.endswith("\n"):
        code_block += "\n"
    window.run_command("terminus_send_string", {"string": code_block, "tag": JULIA_REPL_TAG})
    # return focus to the sending window
    if return_focus:
        window.focus_view(return_focus)


def versioned_text_document_position_params(view: sublime.View, location: int) -> VersionedTextDocumentPositionParams:
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
    # Add another newline before horizontal rule following fenced code block
    content = re.sub("```\n---", "```\n\n---", content)
    # Add another newline before list items
    content = re.sub("\n- ", "\n\n- ", content)
    # Replace [`title`](@ref) links with the corresponding command to navigate the documentation with a new search query
    content = re.sub(
        r"\[`(.+?)`\]\(@ref.*?\)", r"""<a href='subl:julia_search_documentation {"word": "\1"}'>`\1`</a>""", content)
    # Remove parameters after fenced code block language identifier
    content = re.sub("```julia;.*?\n", "```julia\n", content)
    content = re.sub("```jldoctest;.*?\n", "```jldoctest\n", content)
    return content


def startupinfo():
    if sublime.platform() == "windows":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 11
        return si
    return None


class TestItemStorage:

    def __init__(self, window: sublime.Window) -> None:
        self.window = window
        self.pending_result = False
        self.testitemparams = {}  # type: Dict[str, Dict[str, Any]]
        self.testitemdetails = {}  # type: Dict[str, List[TestItemDetail]]
        self.testerrordetails = {}  # type: Dict[str, List[TestErrorDetail]]
        self.testitemstatus = {}  # type: Dict[str, List[TestserverRunTestitemRequestParamsReturn]]
        self.error_keys = {}  # type: Dict[str, Set[str]]

    def update(self, uri: DocumentUri, params: PublishTestsParams) -> None:
        # Use the filepath instead of the URI as the key for storing the testitems, because on Windows the language
        # server sometimes uses uppercase and sometimes lowercase drive letters in the URI for the same file.
        filepath = parse_uri(uri)[1]
        testitems = params['testitemdetails']
        testerrors = params['testerrordetails']
        old_params = self.testitemparams.get(filepath)
        if not testitems and not testerrors:
            # If there are no testitems reported, just delete the key for the previously stored items if they existed.
            if old_params:
                del self.testitemparams[filepath]
                del self.testitemdetails[filepath]
                del self.testerrordetails[filepath]
                del self.testitemstatus[filepath]
                self.render_testitems(uri)
            return
        status = [{
            'status': TestItemStatus.Undetermined,
            'message': None,
            'duration': None
        } for _ in testitems]  # type: List[TestserverRunTestitemRequestParamsReturn]
        if not old_params or \
            any(old_params[key] != params[key] for key in ('project_path', 'package_path', 'package_name')):
            # If there were no testitems for this file already stored, or one of the major parameters changed, copy the
            # new parameters and testitems.
            self.testitemparams[filepath] = {
                'uri': params['uri'],
                'version': params.get('version', 0),
                'project_path': params['project_path'],
                'package_path': params['package_path'],
                'package_name': params['package_name']
            }
        else:
            # If there are both new and old testitems, compare them and determine the unchanged items so that the old
            # status is not forgotten. An old and a new testitem is considered the same if it has the same "id".
            # Unfortunately the "id" field for the testitems is not necessarily unique, so there might be incorrect
            # matches via this approach. Perhaps it should be considered as an additional requirement that the "code"
            # property must also be the same (that would mean that testitems lose their status whenever there are
            # changes in the particular testitem code)...
            self.testitemparams[filepath]['uri'] = params['uri']
            self.testitemparams[filepath]['version'] = \
                params.get('version', self.testitemparams[filepath]['version'] + 1)
            for old_idx, old_item in enumerate(self.testitemdetails[filepath]):
                for new_idx, new_item in enumerate(testitems):
                    if old_item['id'] == new_item['id']:
                        # Copy old status into new status for this testitem
                        status[new_idx] = self.testitemstatus[filepath][old_idx]
                        break
        self.testitemdetails[filepath] = testitems
        self.testerrordetails[filepath] = testerrors
        self.testitemstatus[filepath] = status
        self.render_testitems(uri)

    def stored_version(self, uri: DocumentUri) -> Optional[int]:
        filepath = parse_uri(uri)[1]
        params = self.testitemparams.get(filepath)
        if params:
            return params['version']
        return None

    def render_testitems(self, uri: DocumentUri, new_result_idx: Optional[int] = None) -> None:
        filepath = parse_uri(uri)[1]
        view = self.window.find_open_file(filepath)  # This doesn't work if the tab was dragged out of the window...
        if view and not view.is_loading():
            regions_by_status = {
                TestItemStatus.Passed: [],
                TestItemStatus.Failed: [],
                TestItemStatus.Errored: [],
                TestItemStatus.Undetermined: [],
                TestItemStatus.Pending: [],
                TestItemStatus.Invalid: []
            }  # type: Dict[TestItemStatus, List[sublime.Region]]
            if filepath not in self.testitemdetails:
                for status in regions_by_status:
                    view.erase_regions('lsp_julia_testitem_{}'.format(status))
                return
            annotations = {
                TestItemStatus.Passed: [],
                TestItemStatus.Failed: [],
                TestItemStatus.Errored: [],
                TestItemStatus.Undetermined: [],
                TestItemStatus.Pending: [],
                TestItemStatus.Invalid: []
            }  # type: Dict[TestItemStatus, List[str]]
            version = self.testitemparams[filepath]['version']
            error_annotation_color = view.style_for_scope(TESTITEM_SCOPES[TestItemStatus.Errored])['foreground']
            for idx, item, result in \
                    zip(itertools.count(), self.testitemdetails[filepath], self.testitemstatus[filepath]):
                region = sublime.Region(point_to_offset(Point.from_lsp(item['range']['start']), view))
                annotation = '<a href="{}#idx={}&amp;version={}">Run Test</a>'.format(html.escape(uri), idx, version)
                duration = result['duration']
                if duration is not None:
                    if duration < 100:
                        annotation += " ({}ms)".format(round(duration))
                    else:
                        annotation += " ({:0.2f}s)".format(duration/1000)
                status = cast(TestItemStatus, result['status'])
                regions_by_status[status].append(region)
                if status in (TestItemStatus.Passed, TestItemStatus.Undetermined):
                    annotations[status].append(annotation)
                elif status == TestItemStatus.Pending:
                    annotations[status].append('Running…')
                elif status in (TestItemStatus.Failed, TestItemStatus.Errored):
                    annotations[status].append(annotation)
                    if idx == new_result_idx and result['message'] is not None:
                        for error_idx, message in enumerate(result['message']):
                            location = message['location']
                            if location:
                                regions_key = 'lsp_julia_testitem_error_{}_{}'.format(item['id'], error_idx)
                                view.add_regions(
                                    regions_key,
                                    [range_to_region(location['range'], view)],
                                    flags=sublime.HIDE_ON_MINIMAP | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
                                    annotations=["<br>".join(html.escape(message['message']).split("\n"))],
                                    annotation_color=error_annotation_color,
                                    on_close=partial(self.hide_annotation, uri, regions_key))
                                self.error_keys[filepath].add(regions_key)
                elif status == TestItemStatus.Invalid:
                    annotations[status].append('<br>'.join([
                        "The test process crashed while running this testitem.",
                        "Please check the console and consider to create an issue report in the LSP-julia GitHub repo."
                    ]))
            for testerror in self.testerrordetails[filepath]:
                region = sublime.Region(point_to_offset(Point.from_lsp(testerror['range']['start']), view))
                regions_by_status[TestItemStatus.Invalid].append(region)
                annotations[TestItemStatus.Invalid].append(testerror['error'])
            for status, regions in regions_by_status.items():
                regions_key = 'lsp_julia_testitem_{}'.format(status)
                if regions:
                    on_navigate = self.run_testitem if status not in (TestItemStatus.Pending, TestItemStatus.Invalid) \
                        else None
                    view.add_regions(
                        regions_key,
                        regions,
                        scope=TESTITEM_SCOPES[status],
                        icon=TESTITEM_ICONS[status],
                        flags=sublime.HIDE_ON_MINIMAP | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
                        annotations=annotations[status],
                        annotation_color=view.style_for_scope(TESTITEM_SCOPES[status])['foreground'],
                        on_navigate=on_navigate)
                else:
                    view.erase_regions(regions_key)

    def hide_annotation(self, uri: DocumentUri, key: str) -> None:
        filepath = parse_uri(uri)[1]
        view = self.window.find_open_file(filepath)
        if view:
            view.erase_regions(key)
            self.error_keys[filepath].discard(key)

    def clear_error_annotations(self, uri: DocumentUri, testitem_id: Optional[str] = "") -> None:
        filepath = parse_uri(uri)[1]
        view = self.window.find_open_file(filepath)
        if view:
            for key in self.error_keys.setdefault(filepath, set()).copy():
                if key.startswith('lsp_julia_testitem_error_{}'.format(testitem_id)):
                    view.erase_regions(key)
                    self.error_keys[filepath].discard(key)

    def run_testitem_request_params(
        self, uri: DocumentUri, idx: int
    ) -> Optional[TestserverRunTestitemRequestExtendedParams]:
        filepath = parse_uri(uri)[1]
        params = self.testitemparams.get(filepath)
        if not params:
            return None
        testitem = self.testitemdetails[filepath][idx]
        code_range = testitem.get('code_range')
        if not code_range:
            return None
        code = testitem.get('code')
        if not code:
            return None
        project_path = params['project_path']
        package_path = params['package_path']
        package_name = params['package_name']
        if not any([project_path, package_path, package_name]):
            return None
        line = code_range['start']['line']
        column = code_range['start']['character']
        if column == 0:
            line -= 1  # Fix missmatch of start position between initial and subsequent reported testitem notifications
        return {
            'uri': params['uri'],
            'name': testitem['label'],
            'packageName': package_name,
            'useDefaultUsings': testitem.get('option_default_imports') is not False,
            'line': line,
            'column': column,
            'code': code,
            'project_path': project_path,
            'package_path': package_path
        }

    def run_testitem(self, href: str, focus_testitem: bool = False) -> None:
        if self.pending_result:
            self.window.status_message("Another testitem is already running")
            return
        uri, fragment = urldefrag(href)
        filepath = parse_uri(uri)[1]
        pq = parse_qs(fragment)
        idx = int(pq['idx'][0])
        version = int(pq['version'][0])
        if version != self.stored_version(uri):
            # Actually this should never happen in practice, because annotations for the corresponding view are redrawn
            # on each julia/publishTestitems notification.
            self.window.status_message("Version mismatch for testitem params")
            return
        params = self.run_testitem_request_params(uri, idx)
        if params:
            self.pending_result = True
            self.testitemstatus[filepath][idx]['status'] = TestItemStatus.Pending
            thread = threading.Thread(
                target=self.run_testitem_daemon_thread, args=(uri, idx, version, params), daemon=True)
            thread.start()
            if focus_testitem:
                view = self.window.open_file(
                    "{}:{}".format(filepath, params['line'] + 1), flags=sublime.ENCODED_POSITION)
                # In case the file wasn't open before and is still loading, add a small delay before drawing the
                # annotations.
                if view.is_loading():
                    sublime.set_timeout(partial(self.render_testitems, uri), 50)
                    sublime.set_timeout(partial(self.clear_error_annotations, uri, params['name']), 50)
                    return
            self.render_testitems(uri)
            self.clear_error_annotations(uri, params['name'])

    def run_testitem_daemon_thread(
        self, uri: DocumentUri, idx: int, version: int, params: TestserverRunTestitemRequestExtendedParams
    ) -> None:
        try:
            file_directory = os.path.dirname(parse_uri(params['uri'])[1])
            params_json = json.dumps(params, separators=(',', ':'))
            result_json = subprocess.check_output([
                LspJuliaPlugin.julia_exe(),
                "--startup-file=no",
                "--history-file=no",
                "--project={}".format(LspJuliaPlugin.testrunnerdir()),
                os.path.join(LspJuliaPlugin.testrunnerdir(), "runtestitem.jl"),
                params_json
            ], cwd=file_directory, startupinfo=startupinfo()).decode("utf-8")
            result = json.loads(result_json)
            sublime.set_timeout(partial(self.on_result, uri, idx, version, result))
        except Exception:
            self.pending_result = False
            filepath = parse_uri(uri)[1]
            self.testitemstatus[filepath][idx]['status'] = TestItemStatus.Invalid
            traceback.print_exc()
            sublime.set_timeout(partial(self.render_testitems, uri))

    def on_result(
        self, uri: DocumentUri, idx: int, version: int, params: TestserverRunTestitemRequestParamsReturn
    ) -> None:
        self.pending_result = False
        filepath = parse_uri(uri)[1]
        if self.testitemparams[filepath]['version'] == version:
            self.testitemstatus[filepath][idx] = params
            self.render_testitems(uri, idx)
        else:
            # Ignore result if the language server has notified about a new version of testitems for this file in the
            # meantime. The index of the stored testitem might have changed! Search through all testitems for an item
            # with "pending" status and reset status if found. Don't draw new error annotations.
            for idx, status in enumerate(self.testitemstatus[filepath]):
                if status['status'] == TestItemStatus.Pending:
                    self.testitemstatus[filepath][idx]['status'] = TestItemStatus.Undetermined
            self.render_testitems(uri)


class LspJuliaPlugin(AbstractPlugin):

    def __init__(self, weaksession) -> None:
        super().__init__(weaksession)
        session = weaksession()
        if not session:
            return
        self.testitems = TestItemStorage(session.window)
        env_name = os.path.basename(session.working_directory) if session.working_directory else \
            LspJuliaPlugin.default_julia_environment()
        session.set_config_status_async(env_name)

    @classmethod
    def name(cls) -> str:
        return SESSION_NAME

    @classmethod
    def additional_variables(cls) -> Optional[Dict[str, str]]:
        return {'julia_exe': cls.julia_exe(), 'server_path': cls.serverdir()}

    @classmethod
    def basedir(cls) -> str:
        return os.path.join(cls.storage_path(), "LSP-julia")

    @classmethod
    def serverdir(cls) -> str:
        return os.path.join(cls.basedir(), "languageserver")

    @classmethod
    def testrunnerdir(cls) -> str:
        return os.path.join(cls.basedir(), "testrunner")

    @classmethod
    def version_file(cls) -> str:
        return os.path.join(cls.serverdir(), "VERSION")

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
        return "48ea5bf"  # LanguageServer v4.4.1-DEV

    @classmethod
    def needs_update_or_installation(cls) -> bool:
        # The check for presence of the julia executable is disabled for now, because
        # [`shutil.which`](https://docs.python.org/release/3.3.6/library/shutil.html#shutil.which) doesn't work with
        # symlinks on Python 3.3 (on Windows at least), and if Julia was installed via juliaup it will create a symlink
        # for it - see https://github.com/sublimelsp/LSP-julia/issues/24
        #
        # if not shutil.which(cls.julia_exe()):
        #     msg = ('The executable "{}" could not be found. Set up the path to the Julia executable by running the '
        #         'command\n\n\tPreferences: LSP-julia Settings\n\nfrom the command palette.').format(cls.julia_exe())
        #     raise RuntimeError(msg)
        try:
            with open(cls.version_file(), "r") as fp:
                return cls.server_version() != fp.read().strip()
        except OSError:
            return True

    @classmethod
    def install_or_update(cls) -> None:
        shutil.rmtree(cls.basedir(), ignore_errors=True)
        try:
            os.makedirs(cls.serverdir(), exist_ok=True)
            for file in ("Project.toml", "Manifest.toml"):
                ResourcePath.from_file_path(
                    os.path.join(cls.packagedir(), "server", file)).copy(os.path.join(cls.serverdir(), file))
            # TODO Use cls.basedir() as DEPOT_PATH for language server
            os.makedirs(cls.testrunnerdir(), exist_ok=True)
            for file in ("Project.toml", "runtestitem.jl"):
                ResourcePath.from_file_path(
                    os.path.join(cls.packagedir(), "testrunner", file)).copy(os.path.join(cls.testrunnerdir(), file))
            returncode = subprocess.call([
                cls.julia_exe(),
                "--startup-file=no",
                "--history-file=no",
                "--project={}".format(cls.serverdir()),
                "--eval", "ENV[\"JULIA_SSL_CA_ROOTS_PATH\"] = \"\"; import Pkg; Pkg.instantiate()"
            ])
            if returncode == 0:
                with open(cls.version_file(), "w") as fp:
                    fp.write(cls.server_version())
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
        if method == "textDocument/hover" and isinstance(response.result, dict):
            contents = response.result.get("contents")
            if isinstance(contents, dict) and contents.get("kind") == "markdown":
                response.result["contents"]["value"] = prepare_markdown(contents["value"])

    # Handles the julia/publishTests notification
    def m_julia_publishTests(self, params: PublishTestsParams) -> None:
        if params:
            uri = params['uri']
            self.testitems.update(uri, params)


def plugin_loaded() -> None:
    register_plugin(LspJuliaPlugin)


def plugin_unloaded() -> None:
    unregister_plugin(LspJuliaPlugin)


class LspJuliaOpenFileCommand(sublime_plugin.WindowCommand):
    """ An enhanced version and wrapper of the built-in open_file command, which also supports modifier keys when used
    in form of a link in minihtml. """

    def run(self, event: Optional[dict] = None, **kwargs) -> None:
        if event and kwargs.get('add_to_selection') is None:
            modifier_keys = event.get('modifier_keys', {})
            if 'primary' in modifier_keys or 'shift' in modifier_keys:
                kwargs['add_to_selection'] = True
        self.window.run_command('open_file', kwargs)

    def want_event(self) -> bool:
        return True


SELECT_FOLDER_DIALOG_FLAG = 1


class JuliaActivateEnvironmentCommand(LspWindowCommand):
    """ Selects the active Julia environment, which is used by the language server to resolve the package dependencies
    in order to provide autocomplete suggestions and diagnostics. The active environment will be shown in the status
    bar, unless the "show_environment_status" setting is disabled. """

    session_name = SESSION_NAME

    def run(self, **kwargs) -> None:
        files = kwargs.get('files')
        if files:
            self.activate_environment(os.path.dirname(files[0]))
            return
        env_path = kwargs.get('env_path')
        if env_path == SELECT_FOLDER_DIALOG_FLAG:
            sublime.select_folder_dialog(self.on_select_folder, multi_select=False)  # pyright: ignore
        elif env_path:
            self.activate_environment(env_path)

    def is_visible(self, **kwargs) -> bool:
        if not super().is_enabled():
            return False
        files = kwargs.get('files')
        if files is not None:  # command was invoked from the side bar context menu
            return len(files) == 1 and os.path.basename(files[0]) in ('Project.toml', 'JuliaProject.toml')
        return True

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
        env_name = os.path.basename(env_path)
        session.set_config_status_async(env_name)

    def input(self, args: dict) -> Optional[sublime_plugin.ListInputHandler]:
        if 'files' in args:  # command was invoked from the side bar context menu
            return None
        if 'env_path' not in args:  # command was invoked from the command palette
            session = self.session()
            workspace_folders = session.get_workspace_folders() if session else []
            return EnvPathInputHandler(workspace_folders)


class EnvPathInputHandler(sublime_plugin.ListInputHandler):

    def __init__(self, workspace_folders: List[WorkspaceFolder]) -> None:
        self.workspace_folders = workspace_folders

    def list_items(self) -> List[sublime.ListInputItem]:
        items = []  # type: List[sublime.ListInputItem]
        # Add option for folder picker dialog
        items.append(sublime.ListInputItem("(pick a folder…)", SELECT_FOLDER_DIALOG_FLAG))
        # Collect all folder names and corresponding paths in .julia/environments
        julia_environments_path = os.path.expanduser(os.path.join("~", ".julia", "environments"))
        env_names = [
            env for env in reversed(os.listdir(julia_environments_path))
            if os.path.isdir(os.path.join(julia_environments_path, env))
        ]
        env_paths = [os.path.join(julia_environments_path, env) for env in env_names]
        # Add workspace folders if they are valid Julia project environments
        for folder in self.workspace_folders:
            if folder.path not in env_paths and is_julia_environment(folder.path):
                items.append(sublime.ListInputItem(folder.name, folder.path, kind=KIND_WORKSPACE_FOLDER))
        # Add default Julia environments from .julia/environments
        items.extend([
            sublime.ListInputItem(name, path, kind=KIND_DEFAULT_ENVIRONMENT) for name, path in zip(env_names, env_paths)
        ])
        return items

    def placeholder(self) -> str:
        return "Select Julia environment"

    def preview(self, value: Union[str, int, None]) -> Union[sublime.Html, str, None]:
        if value == SELECT_FOLDER_DIALOG_FLAG:
            return "Open a folder picker dialog to select a Julia project"
        elif value:
            return sublime.Html("<i>{}</i>".format(value))

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
        session = self.session_by_name(self.session_name)
        if session:
            session.send_request(Request("julia/getCurrentBlockRange", params), self.on_result)

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
            session = self.session_by_name(self.session_name)
            if session:
                session.send_request(Request("julia/getCurrentBlockRange", params), self.on_result)
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
        session = self.session()
        if session:
            session.send_request(Request("julia/getDocFromWord", {"word": word}), self.on_result)

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
            if ST_VERSION >= 4135:
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

        toolbar_links.append(
            "<a title='Go back one page' href='{}'>Back</a>".format(
                sublime.command_url('julia_search_documentation', {'word': '__back'})
            ) if self._last_words else "Back")
        toolbar_links.append(
            "<a title='Go forward one page' href='{}'>Forward</a>".format(
                sublime.command_url('julia_search_documentation', {'word': '__forward'})
            ) if self._next_words else "Forward")
        toolbar_links.append("<a href='subl:julia_search_documentation'>Search</a>")
        toolbar = "<div class='toolbar'>" + " | ".join(toolbar_links) + "</div><hr>\n"

        markdown_content = prepare_markdown(response)

        # Replace Markdown links with `file:` URI with actual HTML links and `subl:open_file` command, because the
        # `file:` protocol is not supported for links in minihtml and there is no way to utilize a callback function for
        # links in a HtmlSheet
        if ST_VERSION >= 4127:
            # The `encoded_position` argument for the open_file command was introduced in ST 4127
            # https://github.com/sublimehq/sublime_text/issues/4800
            markdown_content = re.sub(
                r"\[(.+?:\d+)\]\(file:///.+?#\d+\)",
                r"""<a href='subl:lsp_julia_open_file {"file": "\1", "encoded_position": true}'>\1</a>""",
                markdown_content)
        else:
            markdown_content = re.sub(
                r"\[(.+?)(:\d+)\]\(file:///.+?#\d+\)",
                r"""<a href='subl:lsp_julia_open_file {"file": "\1"}'>\1\2</a>""",
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
        return bool(self.view.classify(pt) & (sublime.CLASS_WORD_START | sublime.CLASS_WORD_END | CLASS_INSIDE_WORD))

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


class JuliaRunTestitemCommand(LspWindowCommand):

    session_name = SESSION_NAME

    def run(self) -> None:
        session = self.session()
        if not session:
            return
        plugin = cast(LspJuliaPlugin, session._plugin)
        items = []  # type: List[sublime.QuickPanelItem]
        self.hrefs = []  # type: List[str]
        for filepath, details in plugin.testitems.testitemdetails.items():
            for idx, testitem in enumerate(details):
                status = cast(TestItemStatus, plugin.testitems.testitemstatus[filepath][idx]['status'])
                kind = TESTITEM_KINDS.get(status, sublime.KIND_AMBIGUOUS)
                details = ", ".join(testitem.get('option_tags') or [])
                location = "{}:{}".format(filepath, testitem['range']['start']['line'] + 1)
                items.append(
                    sublime.QuickPanelItem(testitem['label'], details=details, annotation=location, kind=kind))
                uri = plugin.testitems.testitemparams[filepath]['uri']
                version = plugin.testitems.testitemparams[filepath]['version']
                self.hrefs.append("{}#idx={}&amp;version={}".format(uri, idx, version))
        session.window.show_quick_panel(items, on_select=partial(self._on_select, plugin), placeholder="Run @testitem")

    def is_enabled(self) -> bool:
        session = self.session()
        if session is None:
            return False
        plugin = cast(LspJuliaPlugin, session._plugin)
        if plugin.testitems.pending_result:
            return False
        return any(testitem for testitems in plugin.testitems.testitemdetails.values()
                   for testitem in testitems
                   if not testitem.get('error'))

    def _on_select(self, plugin: LspJuliaPlugin, idx: int) -> None:
        if idx > -1:
            href = self.hrefs[idx]
            plugin.testitems.run_testitem(href, focus_testitem=True)
