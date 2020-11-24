from LSP.plugin import AbstractPlugin, ClientConfig, Notification, Request, Session, WorkspaceFolder, register_plugin, unregister_plugin
from LSP.plugin.execute_command import LspExecuteCommand
from LSP.plugin.core.protocol import Point
from LSP.plugin.core.registry import LspTextCommand
# from LSP.plugin.core.registry import best_session, sessions_for_view
from LSP.plugin.core.typing import Any, Dict, List, Optional
from LSP.plugin.core.views import text_document_position_params, point_to_offset
import importlib
import os
import shutil
import sublime
import sublime_plugin
import subprocess


SETTINGS_FILE = "LSP-julia.sublime-settings"
STATUS_BAR_KEY = "lsp_julia"
JULIA_REPL_NAME = "Julia REPL"
JULIA_REPL_TAG = "julia_repl"
CELL_DELIMITERS = ("##", r"#%%", r"# %%")


def start_julia_repl(window: sublime.Window, focus: bool) -> None:
    """
    Start Julia REPL in panel via Terminus package.
    """
    settings = sublime.load_settings(SETTINGS_FILE)
    julia_exe = settings.get("julia_executable_path") or "julia"
    cmd = [julia_exe, "--banner=no", "--project"]  # start in current project environment if available
    window.run_command("terminus_open", {
        "cmd": cmd,
        "cwd": "${file_path:${folder}}",
        "panel_name": JULIA_REPL_NAME,
        "focus": focus,
        "tag": JULIA_REPL_TAG,
        "env": settings.get("repl_env_variables")
    })


def ensure_julia_repl(window: sublime.Window) -> bool:
    """
    Start Julia REPL in panel via Terminus package if not already running.
    """
    if not window.find_output_panel(JULIA_REPL_NAME):
        start_julia_repl(window, False)
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


def versioned_text_document_position_params(view: sublime.View, location: int) -> Dict[str, Any]:
    """
    Custom Julia-specific extension to LSP.

    @see https://github.com/julia-vscode/LanguageServer.jl/blob/master/src/extensions/extensions.jl
    """
    params = text_document_position_params(view, location)
    params["version"] = view.change_count()
    return params


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
            return os.path.basename(folder_path)
        else:
            folder_path = os.path.dirname(folder_path)
    return None


class JuliaLanguageServer(AbstractPlugin):

    def __init__(self, weaksession: 'weakref.ref[Session]') -> None:
        super().__init__(weaksession)
        settings = sublime.load_settings(SETTINGS_FILE)
        if settings.get("show_environment_status"):
            session = weaksession()
            # TODO: use the folder of the initiating view to find the Julia project environment name, because we use
            #       that folder as the working directory of the language server in on_pre_start, which itself is used
            #       by the server to find the Julia project environment. 
            workspace_folders = session.get_workspace_folders()
            if workspace_folders:
                env_name = find_julia_environment(workspace_folders[0].path) or JuliaLanguageServer.default_julia_environment()
                session.set_window_status_async(STATUS_BAR_KEY, env_name)
            else:
                # TODO: how to get the Julia project environment without workspace folders?
                session.set_window_status_async(STATUS_BAR_KEY, "Single File Mode")

    @classmethod
    def name(cls) -> str:
        return "julia"

    @classmethod
    def additional_variables(cls) -> Optional[Dict[str, str]]:
        variables = dict()
        variables["julia_exe"] = cls.julia_exe()
        variables["sysimage_path"] = cls.sysimage_path()
        return variables

    @classmethod
    def basedir(cls) -> str:
        return os.path.join(cls.storage_path(), cls.name())

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
        # return "3.2.0"
        return "f5911cb"

    @classmethod
    def sysimage_path(cls) -> str:
        file_extension = {"windows": ".dll", "linux": ".so", "osx": ".dylib"}[sublime.platform()]
        return os.path.join(cls.basedir(), "Julia-{}-LanguageServer-{}{}".format(cls.julia_version(), cls.server_version(), file_extension))

    @classmethod
    def needs_update_or_installation(cls) -> bool:
        if not shutil.which(cls.julia_exe()):
            msg = "The executable \"{}\" could not be found. Set up the path to the Julia executable by running the command\n\n\tPreferences: LSP-Julia Settings\n\nfrom the command palette.".format(cls.julia_exe())
            raise RuntimeError(msg)
        return not os.path.isfile(cls.sysimage_path())

    @classmethod
    def install_or_update(cls) -> None:
        shutil.rmtree(cls.basedir(), ignore_errors=True)
        os.makedirs(cls.basedir(), exist_ok=True)
        sublime.active_window().status_message("Precompiling Julia Language Server...")
        # TODO: maybe add a user dialog first, because the precompilation takes several minutes and the resulting file size will be ~200MB
        returncode = subprocess.call([cls.julia_exe(), "--startup-file=no", "--history-file=no", os.path.join(cls.packagedir(), "precompile.jl"), cls.packagedir(), cls.sysimage_path()])
        if returncode == 0:
            sublime.active_window().status_message("The Julia Language Server was successfully precompiled into a sysimage.")
        else:
            sublime.error_message("An error occured while precompiling the Julia Language Server.")

    @classmethod
    def on_pre_start(cls, window: sublime.Window, initiating_view: sublime.View, workspace_folders: List[WorkspaceFolder], configuration: ClientConfig) -> Optional[str]:
        # set the working directory of the language server to the directory of the initiating view, because the server uses
        # its working directory to find the Julia project environment if not explicitly given in the starting arguments
        file_path = initiating_view.file_name()  # TODO: maybe use workspace folder instead?
        if file_path:
            return os.path.dirname(file_path)

    # @classmethod
    # def on_post_start(cls, window: sublime.Window, initiating_view: sublime.View, workspace_folders: List[WorkspaceFolder], configuration: ClientConfig) -> None:
    #     settings = sublime.load_settings(SETTINGS_FILE)
    #     if settings.get("show_environment_status"):
    #         file_path = initiating_view.file_name()
    #         if file_path:
    #             folder_path = os.path.dirname(file_path)
    #             julia_environment = find_julia_environment(folder_path) or cls.default_julia_environment()
    #             session = best_session(sessions_for_view(initiating_view), 0)
    #             if session:
    #                 session.set_window_status_async(STATUS_BAR_KEY, julia_environment)


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

    def run(self, edit, env_path):
        session = self.session_by_name(self.session_name)
        session.send_notification(Notification("julia/activateenvironment", env_path))
        settings = sublime.load_settings(SETTINGS_FILE)
        if settings.get("show_environment_status"):
            env_name = os.path.basename(env_path)
            session.set_window_status_async(STATUS_BAR_KEY, env_name)

    def input(self, args):
        if "env_path" not in args:
            session = self.session_by_name(self.session_name)
            workspace_folders = session.get_workspace_folders()
            return EnvPathInputHandler(workspace_folders)


class EnvPathInputHandler(sublime_plugin.ListInputHandler):
    """
    Used by JuliaActivateEnvironmentCommand to display the available Julia project environments the user can choose from.
    """

    def __init__(self, workspace_folders):
        self.workspace_folders = workspace_folders

    def list_items(self):
        # add default Julia environments from .julia/environments
        julia_env_home = os.path.expanduser(os.path.join("~", ".julia", "environments"))
        julia_env_names = [env for env in os.listdir(julia_env_home) if os.path.isdir(os.path.join(julia_env_home, env))]
        julia_env_paths = [os.path.join(julia_env_home, env) for env in julia_env_names]
        julia_environments = [list(env) for env in zip(julia_env_names, julia_env_paths)]
        # add workspace folders if they are valid Julia project environments
        for workspace_folder in reversed(self.workspace_folders):
            if workspace_folder.path not in julia_env_paths and is_julia_environment(workspace_folder.path):
                julia_environments.insert(0, [workspace_folder.name, workspace_folder.path])
        return julia_environments

    def placeholder(self):
        return "Select Julia project/environment folder"

    def preview(self, value):
        return sublime.Html("<i>{}</i>".format(value)) if value else None

    def validate(self, value):
        return value is not None


class JuliaOpenReplCommand(sublime_plugin.WindowCommand):
    """
    Start a Julia REPL via the Terminus package, or focus panel if already started.
    """

    def is_enabled(self) -> bool:
        return importlib.find_loader("Terminus") is not None

    def run(self) -> None:
        repl_view = self.window.find_output_panel(JULIA_REPL_NAME)
        if repl_view:
            self.window.run_command("show_panel", {"panel": "output.{}".format(JULIA_REPL_NAME)})
            self.window.focus_view(repl_view)
        else:
            start_julia_repl(self.window, True)


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
        self.view.sel().clear()
        self.view.run_command("lsp_selection_add", {"regions": [(a, b)]})
        # self.view.show_at_center(sublime.Region(a, b))


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
        repl_ready = ensure_julia_repl(window)  # ensure that Terminus output panel for Julia REPL is available
        sel = self.view.sel()[0]
        if sel.empty():
            params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
            session = self.session_by_name(self.session_name)
            session.send_request(Request("julia/getCurrentBlockRange", params), self.on_result)
        else:
            code_block = self.view.substr(sel)
            if repl_ready:
                send_julia_repl(window, code_block)
            else:
                # give Terminus a bit time to initialize, otherwise the terminus_send_string command doesn't work
                sublime.set_timeout(lambda: send_julia_repl(window, code_block), 5)

    def on_result(self, params: Any) -> None:
        a = point_to_offset(Point.from_lsp(params[0]), self.view)
        b = point_to_offset(Point.from_lsp(params[1]), self.view)
        c = point_to_offset(Point.from_lsp(params[2]), self.view)
        code_block = self.view.substr(sublime.Region(a, b))
        self.view.sel().clear()
        self.view.run_command("lsp_selection_add", {"regions": [(c, c)]})  # move cursor to next code block
        self.view.show_at_center(c)
        send_julia_repl(self.view.window(), code_block)


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
            self.view.run_command("lsp_selection_add", {"regions": [(c, c)]})  # TODO: find out why this was required or replace with simpler solution
            self.view.show_at_center(c)
        else:
            code_block = self.view.substr(sel)
        if repl_ready:
            send_julia_repl(window, code_block)
        else:
            sublime.set_timeout(lambda: send_julia_repl(window, code_block), 5)


class JuliaExecuteCommand(LspExecuteCommand):
    session_name = "julia"  # TODO: this is currently ignored, because capability is defined in LspExecuteCommand
