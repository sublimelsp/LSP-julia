from __future__ import annotations
from LSP.plugin import AbstractPlugin
from LSP.plugin import ClientConfig
from LSP.plugin import css
from LSP.plugin import LspTextCommand
from LSP.plugin import LspWindowCommand
from LSP.plugin import Notification
from LSP.plugin import parse_uri
from LSP.plugin import Request
from LSP.plugin import Response
from LSP.plugin import Session
from LSP.plugin import WorkspaceFolder
from LSP.plugin import register_plugin, unregister_plugin
from LSP.plugin.core.protocol import DocumentUri
from LSP.plugin.core.protocol import Point
from LSP.plugin.core.protocol import Position
from LSP.plugin.core.protocol import Range
from LSP.plugin.core.protocol import TextDocumentIdentifier
from LSP.plugin.core.views import point_to_offset
from LSP.plugin.core.views import text_document_position_params
from LSP.plugin.core.views import uri_from_view
from collections import deque
from sublime_lib import ResourcePath
from typing import Any, Dict, List, TypedDict
from typing_extensions import NotRequired
import importlib
import mdpopups
import os
import re
import shutil
import sublime
import sublime_plugin
import subprocess
import toml


# https://github.com/julia-vscode/julia-vscode/blob/main/src/interactive/misc.ts
# https://github.com/julia-vscode/LanguageServer.jl/blob/master/src/extensions/extensions.jl
class VersionedTextDocumentPositionParams(TypedDict):
    textDocument: TextDocumentIdentifier
    version: int
    position: Position

class TestItemDetail(TypedDict):
    id: str
    label: str
    range: Range
    code: str
    codeRange: Range
    optionDefaultImports: bool
    optionTags: List[str]
    optionSetup: List[str]

class TestSetupDetail(TypedDict):
    name: str
    kind: str
    range: Range
    code: str
    codeRange: Range

class TestErrorDetail(TypedDict):
    id: str
    label: str
    range: Range
    error: str

class PublishTestsParams(TypedDict):
    uri: DocumentUri
    version: NotRequired[int]
    testItemDetails: List[TestItemDetail]
    testSetupDetails: List[TestSetupDetail]
    testErrorDetails: List[TestErrorDetail]


# sublime.Kind tuples for the "Change Current Environment" QuickPanelItems
KIND_DEFAULT_ENVIRONMENT = (sublime.KIND_ID_COLOR_YELLOWISH, "d", "Default Environment")
KIND_WORKSPACE_FOLDER = (sublime.KIND_ID_COLOR_PURPLISH, "f", "Workspace Folder")

# unnamed PointClassification flags for View.classify (no guarantee of correctness)
CLASS_INSIDE_WORD = 512
# CLASS_BRACKET_OPEN = 4096
# CLASS_BRACKET_CLOSE = 8192

ST_VERSION = int(sublime.version())  # This API function is allowed to be invoked at importing time
INSTALLED_PACKAGES_PATH = sublime.installed_packages_path()
PACKAGES_PATH = sublime.packages_path()
SETTINGS_FILE = "LSP-julia.sublime-settings"
SESSION_NAME = "julia"
STATUS_BAR_KEY = "lsp_julia_environment"
JULIA_REPL_NAME = "Julia REPL"
JULIA_REPL_TAG = "julia_repl"
CELL_DELIMITERS = ("##", r"#%%", r"# %%")


def find_output_view(window: sublime.Window, name: str) -> sublime.View | None:
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


def find_julia_environment(folder_path: str) -> str | None:
    """
    Search through parent directories for a Julia project environment.
    """
    while os.path.basename(folder_path):
        if is_julia_environment(folder_path):
            return folder_path
        else:
            folder_path = os.path.dirname(folder_path)
    return None


def find_project_file(folder_path: str) -> str | None:
    """ Search through parent directories for a Project.toml or JuliaProject.toml file. """
    while os.path.basename(folder_path):
        project_file = os.path.join(folder_path, 'JuliaProject.toml')
        if os.path.isfile(project_file):
            return project_file
        project_file = os.path.join(folder_path, 'Project.toml')
        if os.path.isfile(project_file):
            return project_file
        folder_path = os.path.dirname(folder_path)
    return None


def set_environment_status(session: Session, env_path) -> None:
    project_file = find_project_file(env_path)
    if not project_file:
        session.set_config_status_async(os.path.basename(env_path))
        return
    env_path = os.path.dirname(project_file)
    try:
        project_name = toml.load(project_file).get('name')
        if project_name:
            session.set_config_status_async(project_name + '.jl')
            return
        parent_project_file = find_project_file(os.path.dirname(env_path))
        if not parent_project_file:
            session.set_config_status_async(os.path.basename(env_path))
            return
        parent_project_name = toml.load(parent_project_file).get('name')
        if not parent_project_name:
            session.set_config_status_async(os.path.basename(env_path))
            return
        relpath = os.path.relpath(env_path, os.path.dirname(parent_project_file))
        session.set_config_status_async(parent_project_name + '.jl/' + relpath.replace('\\', '/'))
    except toml.TomlDecodeError:
        session.set_config_status_async(os.path.basename(env_path))


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


class LspJuliaPlugin(AbstractPlugin):

    def __init__(self, weaksession) -> None:
        super().__init__(weaksession)
        session = weaksession()
        if not session:
            return
        # self.testitems = TestItemStorage(session.window)
        if session.working_directory and find_project_file(session.working_directory):
            set_environment_status(session, session.working_directory)
        else:
            session.set_config_status_async(LspJuliaPlugin.default_julia_environment())

    @classmethod
    def name(cls) -> str:
        return SESSION_NAME

    @classmethod
    def additional_variables(cls) -> Dict[str, str] | None:
        return {'julia_exe': cls.julia_exe(), 'server_path': cls.basedir()}

    @classmethod
    def basedir(cls) -> str:
        return os.path.join(cls.storage_path(), "LSP-julia")

    @classmethod
    def testrunnerdir(cls) -> str:
        return os.path.join(cls.basedir(), "testrunner")

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
        return "881ada8"  # LanguageServer v5.0.0-DEV

    @classmethod
    def needs_update_or_installation(cls) -> bool:
        if not shutil.which(cls.julia_exe()):
            msg = ('The executable "{}" could not be found. Set up the path to the Julia executable by running the '
                'command\n\n\tPreferences: LSP-julia Settings\n\nfrom the command palette.').format(cls.julia_exe())
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
            for file in ("Project.toml", "Manifest.toml", "Manifest-v1.11.toml", "Manifest-v1.12.toml"):
                ResourcePath.from_file_path(
                    os.path.join(cls.packagedir(), "server", file)).copy(os.path.join(cls.basedir(), file))
            # TODO Use cls.basedir() as DEPOT_PATH for language server
            # os.makedirs(cls.testrunnerdir(), exist_ok=True)
            # for file in ("Project.toml", "runtestitem.jl"):
            #     ResourcePath.from_file_path(
            #         os.path.join(cls.packagedir(), "testrunner", file)).copy(os.path.join(cls.testrunnerdir(), file))
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
    ) -> str | None:
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
        pass
        # if params:
        #     uri = params['uri']
        #     self.testitems.update(uri, params)


def plugin_loaded() -> None:
    register_plugin(LspJuliaPlugin)


def plugin_unloaded() -> None:
    unregister_plugin(LspJuliaPlugin)


class LspJuliaOpenFileCommand(sublime_plugin.WindowCommand):
    """ An enhanced version and wrapper of the built-in open_file command, which also supports modifier keys when used
    in form of a link in minihtml. """

    def run(self, event: dict | None = None, **kwargs) -> None:
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
            curr_file = self.window.active_view().file_name()  # pyright: ignore[reportOptionalMemberAccess]
            starting_dir = os.path.dirname(curr_file) if curr_file else None
            sublime.select_folder_dialog(
                self.on_select_folder, starting_dir, multi_select=False)  # pyright: ignore[reportArgumentType]
        elif env_path:
            self.activate_environment(env_path)

    def is_visible(self, **kwargs) -> bool:
        if not super().is_enabled():
            return False
        files = kwargs.get('files')
        if files is not None:  # command was invoked from the side bar context menu
            return len(files) == 1 and os.path.basename(files[0]) in ('Project.toml', 'JuliaProject.toml')
        return True

    def on_select_folder(self, folder_path: str | None) -> None:
        if folder_path:
            if is_julia_environment(folder_path):
                self.activate_environment(folder_path)
            else:
                self.window.status_message("The selected folder is not a valid Julia environment")

    def activate_environment(self, env_path: str) -> None:
        session = self.session()
        if not session:
            return
        session.send_notification(Notification('julia/activateenvironment', {'envPath': env_path}))
        set_environment_status(session, env_path)


    def input(self, args: dict) -> sublime_plugin.ListInputHandler | None:
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
        items: List[sublime.ListInputItem] = []
        # Add option for folder picker dialog
        items.append(sublime.ListInputItem("(pick a folder…)", SELECT_FOLDER_DIALOG_FLAG))
        # Collect all folder names and corresponding paths in .julia/environments
        julia_environments_path = os.path.expanduser(os.path.join("~", ".julia", "environments"))
        env_names = [
            env for env in reversed(os.listdir(julia_environments_path))
            if os.path.isdir(os.path.join(julia_environments_path, env))
        ]
        env_paths = [os.path.join(julia_environments_path, env) for env in env_names]
        # Add all environments withing a working folder.
        for folder in self.workspace_folders:
            for subdir, dirs, files in os.walk(folder.path):
                # exclude all hidden folders in the working folder
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                folderpath = os.path.join(folder.path, subdir)
                if folderpath not in env_paths and is_julia_environment(folderpath):
                    basename = os.path.basename(folder.path)
                    relpath = os.path.relpath(subdir, folder.path)
                    env_name = basename if relpath == '.' else os.path.join(basename, relpath)
                    items.append(sublime.ListInputItem(env_name, folderpath, kind=KIND_WORKSPACE_FOLDER))

        # Add default Julia environments from .julia/environments
        items.extend([
            sublime.ListInputItem(name, path, kind=KIND_DEFAULT_ENVIRONMENT) for name, path in zip(env_names, env_paths)
        ])
        return items

    def placeholder(self) -> str:
        return "Select Julia environment"

    def preview(self, text: str | int | None) -> sublime.Html | str:
        if text == SELECT_FOLDER_DIALOG_FLAG:
            return "Open a folder picker dialog to select a Julia project"
        elif text:
            return sublime.Html("<i>{}</i>".format(text))
        return ""

    def validate(self, text: str | int | None) -> bool:
        return text is not None


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

    def is_enabled(self, event: dict | None = None, point: int | None = None) -> bool:
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

    def run(self, edit: sublime.Edit, event: dict | None = None, point: int | None = None) -> None:
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

    _sheet_id: int | None = None

    _last_words = deque(maxlen=100)
    _next_words = deque()
    _current_word: str | None = None

    def run(self, word: str) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
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
        toolbar_links: List[str] = []

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
                r"\[(.+?:\d+)\]\((file:///.+?)#(\d+)\)",
                self._link_replacement,
                markdown_content)
        else:
            markdown_content = re.sub(
                r"\[(.+?:\d+)\]\((file:///.+?)#\d+\)",
                lambda match: self._link_replacement(match, False),
                markdown_content)

        content = frontmatter + toolbar + markdown_content

        mdpopups.update_html_sheet(sheet, content, css=css().sheets, wrapper_class="lsp_sheet")

    def input(self, args: dict) -> sublime_plugin.TextInputHandler | None:
        if "word" not in args:
            return WordInputHandler()

    def _link_replacement(self, match: re.Match, encoded_position: bool = True) -> str:
        path = parse_uri(match.group(2))[1].replace('\\', '\\\\')
        if encoded_position:
            return """<a href='subl:lsp_julia_open_file {{"file": "{}:{}", "encoded_position": true}}'>{}</a>""".format(
                path, match.group(3), match.group(1))
        return """<a href='subl:lsp_julia_open_file {{"file": "{}"}}'>{}</a>""".format(path, match.group(1))


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

    def is_enabled(self, event: dict | None = None, point: int | None = None) -> bool:
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

    def is_visible(self, event: dict | None = None, point: int | None = None) -> bool:
        return self.is_enabled(event, point)

    def run(self, edit: sublime.Edit, event: dict | None = None, point: int | None = None) -> None:
        if event is not None and "x" in event and "y" in event:
            pt = self.view.window_to_text((event["x"], event["y"]))
        else:
            pt = self.view.sel()[0].b
        # we already know that the point is really on a word due to self.is_enabled
        word = self.view.substr(self.view.word(pt))
        window = self.view.window()
        if window:
            window.run_command("julia_search_documentation", {"word": word})
