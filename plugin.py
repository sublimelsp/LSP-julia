from LSP.plugin import AbstractPlugin, ClientConfig, Notification, Request, WorkspaceFolder, register_plugin, unregister_plugin
from LSP.plugin.execute_command import LspExecuteCommand
from LSP.plugin.core.protocol import Point
from LSP.plugin.core.registry import LspTextCommand
from LSP.plugin.core.typing import Any, Dict, List, Optional, Union
from LSP.plugin.core.views import text_document_position_params, point_to_offset, uri_from_view
from sublime_lib import ResourcePath
import importlib
import os
import shutil
import sublime
import sublime_plugin
import subprocess


SETTINGS_FILE = "LSP-julia.sublime-settings"
STATUS_BAR_KEY = "lsp_julia_environment"
JULIA_REPL_NAME = "Julia REPL"
JULIA_REPL_TAG = "julia_repl"
CELL_DELIMITERS = ("##", r"#%%", r"# %%")

# there isn't a native way of doing this, as far as i know.
def find_output_view(window: sublime.Window, name: str):
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
        "panel_name": panel and JULIA_REPL_NAME or None,
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
    edit_view = window.active_view() 
    # ensure code block ends with newline to enforce execution in REPL
    if not code_block.endswith("\n"):
        code_block += "\n"
    window.run_command("terminus_send_string", {"string": code_block, "tag": JULIA_REPL_TAG})
    window.focus_view(edit_view) #returns the focus to the currently edited view that is sending code.

def versioned_text_document_position_params(view: sublime.View, location: int) -> Dict[str, Any]:
    """
    Custom Julia-specific extension to the LSP.

    @see https://github.com/julia-vscode/LanguageServer.jl/blob/master/src/extensions/extensions.jl
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
    return os.path.isfile(os.path.join(folder_path, "Project.toml")) or os.path.isfile(os.path.join(folder_path, "JuliaProject.toml"))


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


class JuliaLanguageServer(AbstractPlugin):

    def __init__(self, weaksession) -> None:
        super().__init__(weaksession)
        if sublime.load_settings(SETTINGS_FILE).get("show_environment_status"):
            session = self.weaksession()
            if session:
                env_name = os.path.basename(session.working_directory) if session.working_directory else JuliaLanguageServer.default_julia_environment()
                session.set_window_status_async(STATUS_BAR_KEY, "Julia env: {}".format(env_name))

    @classmethod
    def name(cls) -> str:
        return "julia"

    @classmethod
    def additional_variables(cls) -> Optional[Dict[str, str]]:
        variables = dict()
        variables["julia_exe"] = cls.julia_exe()
        variables["server_path"] = os.path.join(cls.basedir(), cls.server_version())
        return variables

    @classmethod
    def basedir(cls) -> str:
        return os.path.join(cls.storage_path(), "LSP-julia")

    @classmethod
    def packagedir(cls) -> str:
        return os.path.join(sublime.packages_path(), "LSP-julia")

    @classmethod
    def julia_exe(cls) -> str:
        return str(sublime.load_settings(SETTINGS_FILE).get("julia_executable_path")) or "julia"

    @classmethod
    def julia_version(cls) -> str:
        return subprocess.check_output([cls.julia_exe(), "--version"]).decode("utf-8").rstrip().split()[-1]

    @classmethod
    def default_julia_environment(cls) -> str:
        major, minor, _ = cls.julia_version().split(".")
        return "v{}.{}".format(major, minor)

    @classmethod
    def server_version(cls) -> str:
        return "dc87d72"  # LanguageServer v4.1.1-DEV

    @classmethod
    def needs_update_or_installation(cls) -> bool:
        if not shutil.which(cls.julia_exe()):
            msg = "The executable \"{}\" could not be found. Set up the path to the Julia executable by running the command\n\n\tPreferences: LSP-julia Settings\n\nfrom the command palette.".format(cls.julia_exe())
            raise RuntimeError(msg)
        return not os.path.isfile(os.path.join(cls.basedir(), cls.server_version(), "ready"))

    @classmethod
    def install_or_update(cls) -> None:
        shutil.rmtree(cls.basedir(), ignore_errors=True)
        serverdir = os.path.join(cls.basedir(), cls.server_version())
        os.makedirs(serverdir, exist_ok=True)
        for file in ["Project.toml", "Manifest.toml"]:
            ResourcePath.from_file_path(os.path.join(cls.packagedir(), "server", file)).copy(os.path.join(serverdir, file))  # type: ignore
        # TODO Use serverdir as DEPOT_PATH
        returncode = subprocess.call([cls.julia_exe(), "--startup-file=no", "--history-file=no", "--project={}".format(serverdir), "--eval", "ENV[\"JULIA_SSL_CA_ROOTS_PATH\"] = \"\"; import Pkg; Pkg.instantiate()"])
        if returncode == 0:
            # create a dummy file to indicate that the installation was successful
            open(os.path.join(serverdir, "ready"), 'a').close()
        else:
            error_msg = "An error occured while trying to install the Language Server. Check the console for possible error messages or consider to open an issue in the LSP-julia issue tracker on GitHub."
            sublime.error_message(error_msg)

    @classmethod
    def on_pre_start(cls, window: sublime.Window, initiating_view: sublime.View, workspace_folders: List[WorkspaceFolder], configuration: ClientConfig) -> Optional[str]:
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


def plugin_loaded() -> None:
    register_plugin(JuliaLanguageServer)


def plugin_unloaded() -> None:
    unregister_plugin(JuliaLanguageServer)


class JuliaActivateEnvironmentCommand(LspTextCommand):
    """
    Can be invoked from the command palette to switch the active Julia project environment.
    The active Julia project environment detemines the Julia packages used by the language server to provide
    autocomplete suggestions and diagnostics.
    """

    session_name = "julia"

    def run(self, edit: sublime.Edit, env_path: str) -> None:
        if env_path == "__select_folder_dialog":
            sublime.select_folder_dialog(self.on_select_folder, multi_select=False)  # type: ignore
        else:
            self.activate_environment(env_path)

    def on_select_folder(self, folder_path: Optional[str]) -> None:
        if folder_path:
            if is_julia_environment(folder_path):
                self.activate_environment(folder_path)
            else:
                sublime.active_window().status_message("The selected folder is not a valid Julia environment")

    def activate_environment(self, env_path: str) -> None:
        session = self.session_by_name(self.session_name)
        if not session:
            return
        session.send_notification(Notification("julia/activateenvironment", {"envPath": env_path}))
        if sublime.load_settings(SETTINGS_FILE).get("show_environment_status"):
            env_name = os.path.basename(env_path)
            session.set_window_status_async(STATUS_BAR_KEY, "Julia env: {}".format(env_name))

    def input(self, args: dict) -> Optional[sublime_plugin.ListInputHandler]:
        if "env_path" not in args:
            session = self.session_by_name(self.session_name)
            workspace_folders = session.get_workspace_folders()
            return EnvPathInputHandler(workspace_folders)


class EnvPathInputHandler(sublime_plugin.ListInputHandler):
    """
    Used by JuliaActivateEnvironmentCommand to display the available Julia project environments the user can choose from.
    """

    def __init__(self, workspace_folders: List[WorkspaceFolder]) -> None:
        self.workspace_folders = workspace_folders

    def list_items(self) -> List[sublime.ListInputItem]:
        # add default Julia environments from .julia/environments
        julia_env_home = os.path.expanduser(os.path.join("~", ".julia", "environments"))
        names = [env for env in os.listdir(julia_env_home) if os.path.isdir(os.path.join(julia_env_home, env))]  # collect all folder names in .julia/environments
        paths = [os.path.join(julia_env_home, env) for env in names]  # the corresponding folder paths
        items = [sublime.ListInputItem(name, path, kind=(sublime.KIND_ID_COLOR_YELLOWISH, "d", "default environment")) for name, path in zip(names, paths)]
        # add workspace folders on top of the list if they are valid Julia project environments
        for workspace_folder in reversed(self.workspace_folders):
            if workspace_folder.path not in paths and is_julia_environment(workspace_folder.path):
                items.insert(0, sublime.ListInputItem(workspace_folder.name, workspace_folder.path, kind=(sublime.KIND_ID_COLOR_PURPLISH, "f", "workspace folder")))
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

    session_name = "julia"

    def run(self, edit: sublime.Edit) -> None:
        params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
        session = self.session_by_name(self.session_name)
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

    session_name = "julia"

    def is_enabled(self) -> bool:
        # language server must be ready
        if not bool(self.session_by_name(self.session_name)):
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
        # ensure that Terminus output panel for Julia REPL is available
        repl_ready = ensure_julia_repl(window)  # type: ignore # view.window() can in theory return None if the tab was closed in the meantime, but thats probably irrelevant here
        sel = self.view.sel()[0]
        if sel.empty():
            params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
            session = self.session_by_name(self.session_name)
            session.send_request(Request("julia/getCurrentBlockRange", params), self.on_result)
        else:
            code_block = self.view.substr(sel)
            if repl_ready:
                send_julia_repl(window, code_block)  # type: ignore
            else:
                # give Terminus a bit time to initialize, otherwise the terminus_send_string command doesn't work
                sublime.set_timeout(lambda: send_julia_repl(window, code_block), 5)  # type: ignore

    def on_result(self, params: Any) -> None:
        a = point_to_offset(Point.from_lsp(params[0]), self.view)
        b = point_to_offset(Point.from_lsp(params[1]), self.view)
        c = point_to_offset(Point.from_lsp(params[2]), self.view)
        code_block = self.view.substr(sublime.Region(a, b))
        self.view.run_command("lsp_selection_set", {"regions": [(c, c)]})  # move cursor to next code block
        self.view.show_at_center(c)
        send_julia_repl(self.view.window(), code_block)  # type: ignore


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
        sel = self.view.sel()[0]
        repl_ready = ensure_julia_repl(window)  # type: ignore
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
            send_julia_repl(window, code_block)  # type: ignore
        else:
            sublime.set_timeout(lambda: send_julia_repl(window, code_block), 5)  # type: ignore


class JuliaExecuteCommand(LspExecuteCommand):
    session_name = "julia"
