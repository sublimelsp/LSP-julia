import os
import re
import importlib
import sublime
import sublime_plugin

from LSP.plugin.core.handlers import LanguageHandler
from LSP.plugin.core.settings import read_client_config
from LSP.plugin.core.protocol import Request, Notification, Point
from LSP.plugin.core.registry import LspTextCommand, session_for_view
from LSP.plugin.core.views import text_document_position_params, versioned_text_document_identifier, point_to_offset
from LSP.plugin.execute_command import LspExecuteCommand

from .utils import load_settings


SETTINGS_FILE = "LSP-julia.sublime-settings"
JULIA_REPL_TAG = "lsp_julia_repl"


def versioned_text_document_position_params(view, location):
    params = text_document_position_params(view, location)
    params["version"] = versioned_text_document_identifier(view)["version"]
    return params


def get_active_environment() -> tuple:
    settings = sublime.load_settings(SETTINGS_FILE)
    command = settings.get("command", [])
    command_str = command[-1]
    regex = re.compile("env_path=raw\".+\";")
    m = regex.findall(command_str)
    if len(m) != 1:
        return ""
    env_path = m[0][13:-2]
    env_name = os.path.basename(env_path)
    return env_name, env_path


def is_project_folder(env_path: str) -> bool:
    return os.path.isfile(os.path.join(env_path, "Project.toml")) or os.path.isfile(os.path.join(env_path, "JuliaProject.toml"))


def update_environment_settings(env_path: str) -> None:
    settings = sublime.load_settings(SETTINGS_FILE)
    command = settings.get("command")
    if command:
        command[-1] = "using LanguageServer, LanguageServer.SymbolServer; env_path=raw\"{}\"; depot_path=first(Base.DEPOT_PATH); server=LanguageServer.LanguageServerInstance(stdin,stdout,env_path,depot_path); run(server)".format(env_path)
        settings.set("command", command)
        sublime.save_settings(SETTINGS_FILE)


def update_environment_status(window: sublime.Window, env_name: str) -> None:
    for view in window.views():
        if view.match_selector(0, "source.julia"):
            view.set_status("lsp_clients_julia", env_name)


class JuliaFileListener(sublime_plugin.EventListener):
    def on_load(self, view: sublime.View) -> None:
        if not view.match_selector(0, "source.julia"):
            return
        settings = sublime.load_settings(SETTINGS_FILE)
        if not settings.get("enabled", True):
            return
        if not settings.get("show_environment_status"):
            return
        if not session_for_view(view, None):
            return
        env_name = get_active_environment()[0]
        if env_name:
            view.set_status("lsp_clients_julia", env_name)


class LspJuliaPlugin(LanguageHandler):
    _window = None

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "julia"

    @property
    def config(self):
        settings = sublime.load_settings(SETTINGS_FILE)
        julia_executable_path = settings.get("julia_executable_path")
        command = settings.get("command")
        command[0] = julia_executable_path or "julia"
        settings.set("command", command)
        sublime.save_settings(SETTINGS_FILE)
        settings = load_settings(SETTINGS_FILE)
        return read_client_config(self.name, settings)

    def on_start(self, window) -> bool:
        self._window = window
        return True

    def on_initialized(self, client) -> None:
        settings = sublime.load_settings(SETTINGS_FILE)
        # TODO: is it possible to do this logic even before the language server starts,
        #       so that it will use the correct env_path right from the beginning?
        if settings.get("auto_change_environment"):
            current_env_path = get_active_environment()[1]
            for folder in self._window.folders():
                if is_project_folder(folder):
                    if folder != current_env_path:
                        client.send_notification(Notification("julia/activateenvironment", folder))
                        update_environment_settings(folder)
                    break
        if settings.get("show_environment_status"):
            env_name = get_active_environment()[0]
            if env_name:
                update_environment_status(self._window, env_name)


class JuliaActivateEnvironmentCommand(LspTextCommand):
    def is_enabled(self):
        return self.view.match_selector(0, "source.julia") and self.client_with_capability(None) is not None

    def run(self, edit, env_path):
        if env_path[-1] in {"/", "\\"}:
            env_path = env_path[0:-1]

        # send julia/activateenvironment notification
        client = self.client_with_capability(None)
        client.send_notification(Notification("julia/activateenvironment", env_path))

        # update settings
        update_environment_settings(env_path)

        # update status bar
        settings = load_settings(SETTINGS_FILE)
        if settings.get("show_environment_status"):
            env_name = os.path.basename(env_path)
            update_environment_status(self.view.window(), env_name)

    def input(self, args):
        return EnvPathInputHandler(self.view)


class EnvPathInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, view):
        self.view = view

    def list_items(self):
        # add folders in .julia/environments
        julia_env_home = os.path.expanduser(os.path.join("~", ".julia", "environments"))
        julia_env_names = [env for env in os.listdir(julia_env_home) if os.path.isdir(os.path.join(julia_env_home, env))]
        julia_env_paths = [os.path.join(julia_env_home, env) for env in julia_env_names]
        julia_env = [list(env) for env in zip(julia_env_names, julia_env_paths)]
        # check and add project folders
        for folder_path in reversed(self.view.window().folders()):
            if folder_path not in julia_env_paths and is_project_folder(folder_path):
                folder_name = os.path.basename(folder_path)
                julia_env.insert(0, [folder_name, folder_path])
        return julia_env

    def placeholder(self):
        return "Select Julia project folder"

    def preview(self, value):
        return sublime.Html("<i>{}</i>".format(value)) if value else None

    def validate(self, value):
        return value is not None


class JuliaSelectCodeBlockCommand(LspTextCommand):
    def is_enabled(self):
        return self.view.match_selector(0, "source.julia") and self.client_with_capability(None) is not None

    def run(self, edit):
        # send julia/getCurrentBlockRange request
        params = text_document_position_params(self.view, self.view.sel()[0].b)
        params["version"] = versioned_text_document_identifier(self.view)["version"]
        client = self.client_with_capability(None)
        client.send_request(Request("julia/getCurrentBlockRange", params), self.handle_response)

    def handle_response(self, response):
        a = point_to_offset(Point.from_lsp(response[0]), self.view)
        b = point_to_offset(Point.from_lsp(response[1]), self.view)
        self.view.sel().clear()
        self.view.run_command("lsp_selection_add", {"regions": [(a, b)]})
        self.view.show_at_center(sublime.Region(a, b))


class JuliaRunCodeBlockCommand(LspTextCommand):
    def is_enabled(self):
        # must be Julia file
        if not self.view.match_selector(0, "source.julia"):
            return False
        # Terminus package must be installed
        if not importlib.find_loader("Terminus"):
            return False
        # Language Server must be ready
        if not self.client_with_capability(None):
            return False
        # cursor must not be at end of file
        if self.view.sel()[0].b == self.view.size():
            return
        return True

    def run(self, edit):
        # ensure that Terminus output panel for Julia REPL is available
        if not self.view.window().find_output_panel("Julia REPL"):
            settings = sublime.load_settings(SETTINGS_FILE)
            julia_executable = settings.get("julia_executable_path") or "julia"
            # start in current project environment if available
            cmd = [julia_executable, "--project"]
            self.view.window().run_command("terminus_open", {
                "cmd": cmd,
                "cwd": "${file_path:${folder}}",
                "panel_name": "Julia REPL",
                "focus": False,
                "tag": JULIA_REPL_TAG
            })
        # send julia/getCurrentBlockRange request
        params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
        client = self.client_with_capability(None)
        client.send_request(Request("julia/getCurrentBlockRange", params), self.handle_response)

    def handle_response(self, response):
        a = point_to_offset(Point.from_lsp(response[0]), self.view)
        b = point_to_offset(Point.from_lsp(response[1]), self.view)
        c = point_to_offset(Point.from_lsp(response[2]), self.view)
        code_block = self.view.substr(sublime.Region(a, b))
        if not code_block.endswith("\n"):
            code_block += "\n"
        # move cursor to next code block
        self.view.sel().clear()
        self.view.run_command("lsp_selection_add", {"regions": [(c, c)]})
        self.view.show_at_center(c)
        # send code block to Terminus Julia REPL
        self.view.window().run_command("terminus_send_string", {"string": code_block, "tag": JULIA_REPL_TAG})


# class JuliaGetDocumentation(LspTextCommand):
#     def is_enabled(self):
#         if not self.view.match_selector(0, "source.julia"):
#             return False
#         if not self.client_with_capability(None):
#             return False
#         return True

#     def run(self, edit):
#         params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
#         client = self.client_with_capability(None)
#         client.send_request(Request("julia/getDocAt", params), self.handle_response)

#     def handle_response(self, response):
#         if not response:
#             self.view.window().status_message("No documentation available at cursor position")


# class JuliaGetModule(LspTextCommand):
#     def is_enabled(self):
#         if not self.view.match_selector(0, "source.julia"):
#             return False
#         if not self.client_with_capability(None):
#             return False
#         return True

#     def run(self, edit):
#         params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
#         client = self.client_with_capability(None)
#         client.send_request(Request("julia/getModuleAt", params), self.handle_response)

#     def handle_response(self, response):
#         self.view.window().status_message(response)


class JuliaOpenReplCommand(LspTextCommand):
    def is_enabled(self):
        if not self.view.match_selector(0, "source.julia"):
            return False
        if not importlib.find_loader("Terminus"):
            return False
        return True

    def run(self, edit):
        repl_view = self.view.window().find_output_panel("Julia REPL")
        if repl_view:
            self.view.window().focus_view(repl_view)
        else:
            settings = sublime.load_settings(SETTINGS_FILE)
            julia_executable = settings.get("julia_executable_path") or "julia"
            # start in current project environment if available
            cmd = [julia_executable, "--project"]
            self.view.window().run_command("terminus_open", {
                "cmd": cmd,
                "cwd": "${file_path:${folder}}",
                "panel_name": "Julia REPL",
                "focus": True,
                "tag": JULIA_REPL_TAG
            })


class JuliaExecuteCommand(LspExecuteCommand):
    def is_enabled(self):
        return self.view.match_selector(0, "source.julia") and self.client_with_capability(None) is not None
