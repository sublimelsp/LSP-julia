import os
import re
import importlib
import subprocess
import threading

import sublime
import sublime_plugin

from LSP.plugin.core.handlers import LanguageHandler
from LSP.plugin.core.settings import read_client_config
from LSP.plugin.core.protocol import Request, Notification, Point
from LSP.plugin.core.registry import LspTextCommand
from LSP.plugin.core.views import text_document_position_params, versioned_text_document_identifier, point_to_offset
from LSP.plugin.execute_command import LspExecuteCommand


SETTINGS_FILE = "LSP-julia.sublime-settings"
STATUS_BAR_KEY = "lsp_julia_environment"
JULIA_REPL_NAME = "Julia REPL"
JULIA_REPL_TAG = "julia_repl"
CELL_DELIMITERS = ("##", r"#%%", r"# %%")


# custom Julia-specific extension to the LSP
# @see https://github.com/julia-vscode/LanguageServer.jl/blob/master/src/extensions/extensions.jl
def versioned_text_document_position_params(view: sublime.View, location: int):
    params = text_document_position_params(view, location)
    params["version"] = versioned_text_document_identifier(view)["version"]
    return params


# read Julia project path from server starting command used in settings file to allow displaying its name in status bar
def get_active_environment():
    settings = sublime.load_settings(SETTINGS_FILE)
    command = settings.get("command", [])
    regex = re.compile("env_path=raw\".+\";")
    m = regex.findall(command[-1])
    if len(m) != 1:
        return None, None
    env_path = m[0][13:-2]
    env_name = os.path.basename(env_path)
    return env_name, env_path


# check whether folder is a Julia project, i.e. it contains a Project.toml or JuliaProject.toml file
def is_project_folder(env_path: str):
    return os.path.isfile(os.path.join(env_path, "Project.toml")) or os.path.isfile(os.path.join(env_path, "JuliaProject.toml"))


# the language server requires to specify the active environment (Julia project) path in its starting command
# @see https://github.com/julia-vscode/LanguageServer.jl/issues/748
def update_starting_command(env_path=None):
    settings = sublime.load_settings(SETTINGS_FILE)
    command = [
        settings.get("julia_executable_path") or "julia",
        "--startup-file=no",
        "--history-file=no"
    ]
    env_path_str = "raw\"{}\"".format(env_path) if env_path else "dirname(Base.load_path_expand(LOAD_PATH[2]))"
    sysimage_path = settings.get("sysimage_path")
    if sysimage_path:
        command.append("--sysimage")
        command.append(sysimage_path)
        command.append("-e")
        command.append("env_path={}; depot_path=get(ENV, \"JULIA_DEPOT_PATH\", \"\"); server=LanguageServer.LanguageServerInstance(stdin,stdout,env_path,depot_path); run(server)".format(env_path_str))
    else:
        command.append("-e")
        command.append("using LanguageServer, LanguageServer.SymbolServer; env_path={}; depot_path=get(ENV, \"JULIA_DEPOT_PATH\", \"\"); server=LanguageServer.LanguageServerInstance(stdin,stdout,env_path,depot_path); run(server)".format(env_path_str))
    settings.set("command", command)
    sublime.save_settings(SETTINGS_FILE)
    return command


# allows to update the Julia project name in the status bar for all Julia files of a window
def update_environment_status(window: sublime.Window, env_name: str):
    for view in window.views():
        if view.match_selector(0, "source.julia"):
            if view.settings().get("lsp_active"):
                view.set_status(STATUS_BAR_KEY, env_name)
            else:
                view.erase_status(STATUS_BAR_KEY)


# start Julia REPL via Terminus package
def start_terminus_repl(window: sublime.Window, focus: bool):
    settings = sublime.load_settings(SETTINGS_FILE)
    julia_executable = settings.get("julia_executable_path") or "julia"
    # start in current project environment if available
    cmd = [julia_executable, "--banner=no", "--project"]
    window.run_command("terminus_open", {
        "cmd": cmd,
        "cwd": "${file_path:${folder}}",
        "panel_name": JULIA_REPL_NAME,
        "focus": focus,
        "tag": JULIA_REPL_TAG,
        "env": settings.get("repl_env_variables")
    })


# start Julia REPL via Terminus package if not already running
def ensure_terminus_repl(window: sublime.Window):
    if not window.find_output_panel(JULIA_REPL_NAME):
        start_terminus_repl(window, False)
        return False
    return True


# send a code block string to Julia REPL via Terminus package
def send_terminus_repl(window: sublime.Window, code_block: str):
    # ensure code block ends with newline, so that it will be executed in REPL
    if not code_block.endswith("\n"):
        code_block += "\n"
    window.run_command("terminus_send_string", {"string": code_block, "tag": JULIA_REPL_TAG})


# add Julia project name in status bar for newly opened files
class JuliaFileListener(sublime_plugin.ViewEventListener):

    @classmethod
    def is_applicable(cls, settings):
        if not settings.get("syntax").endswith("Julia.sublime-syntax"):
            return False
        lsp_settings = sublime.load_settings(SETTINGS_FILE)
        return lsp_settings.get("enabled", True)

    def on_load_async(self):
        settings = sublime.load_settings(SETTINGS_FILE)
        if not settings.get("show_environment_status"):
            return
        env_name = get_active_environment()[0]
        if env_name:
            self.view.set_status(STATUS_BAR_KEY, env_name)


class LspJuliaPlugin(LanguageHandler):
    _window = None

    def __init__(self):
        super().__init__()

    @property
    def name(self):
        return "lsp-julia"

    @property
    def config(self):
        # load default and user configuration separately to allow merging of settings dicts
        client_config = sublime.decode_value(sublime.load_resource("Packages/{}/{}".format(__package__, SETTINGS_FILE)))
        client_config["enabled"] = True
        if os.path.exists(os.path.join(sublime.packages_path(), "User", SETTINGS_FILE)):
            user_config = sublime.decode_value(sublime.load_resource("Packages/User/{}".format(SETTINGS_FILE)))
            # merge settings dict
            settings = client_config.get("settings", {})
            settings.update(user_config.get("settings", {}))
            client_config.update(user_config)
            client_config["settings"] = settings
            # update starting command if server should be started with sysimage,
            # because sysimage_path in user settings might have been changed manually
            if user_config.get("sysimage_path"):
                env_path = get_active_environment()[1]
                command = update_starting_command(env_path)
                client_config["command"] = command
        return read_client_config(self.name, client_config)

    def on_start(self, window):
        self._window = window
        return True

    def on_initialized(self, client):
        settings = sublime.load_settings(SETTINGS_FILE)
        # TODO: is it possible to do this logic even before the language server starts,
        #       so that it will use the correct env_path right from the beginning?
        if settings.get("auto_change_environment"):
            current_env_path = get_active_environment()[1]
            for folder in self._window.folders():
                if is_project_folder(folder):
                    if folder != current_env_path:
                        client.send_notification(Notification("julia/activateenvironment", folder))
                        update_starting_command(folder)
                    break
        if settings.get("show_environment_status"):
            env_name = get_active_environment()[0]
            if env_name:
                update_environment_status(self._window, env_name)


class JuliaPrecompileLanguageServerCommand(sublime_plugin.WindowCommand):
    def run(self, sysimage_path):
        self.window.status_message("Precompiling Julia Language Server...")
        thread = threading.Thread(target=self.precompile, args=[sysimage_path])
        thread.start()

    def input(self, args):
        if "sysimage_path" not in args:
            return SysimagePathInputHandler()

    def input_description(self):
        return "Sysimage path"

    def precompile(self, sysimage_path):
        settings = sublime.load_settings(SETTINGS_FILE)
        julia_bin = settings.get("julia_executable_path") or "julia"
        cache_path = os.path.join(sublime.cache_path(), "JuliaLanguageServer")
        if not os.path.exists(cache_path):
            os.mkdir(cache_path)
        # copy precompile execution file to cache_path
        precompile_execution_file = os.path.join(cache_path, "precompile_execution_file.jl")
        if os.path.exists(precompile_execution_file):
            os.remove(precompile_execution_file)
        file = open(precompile_execution_file, "w")
        file.write(sublime.load_resource("Packages/LSP-julia/precompile_execution_file.jl"))
        file.close()
        # load and execute precompile script
        precompile_script = sublime.load_resource("Packages/LSP-julia/precompile.jl").replace("\n", ";")
        returncode = subprocess.call([julia_bin, "-e", precompile_script, cache_path, sysimage_path])
        if returncode == 0:
            settings.set("sysimage_path", sysimage_path)
            sublime.save_settings(SETTINGS_FILE)
            env_path = get_active_environment()[1]
            update_starting_command(env_path)
            sublime.message_dialog("The language server has successfully been precompiled into a sysimage, which will be used after Sublime Text is restarted.")
        else:
            sublime.error_message("An error occured while precompiling the language server.")


class SysimagePathInputHandler(sublime_plugin.TextInputHandler):
    def initial_text(self):
        file_extension = {
            "windows": "dll",
            "linux": "so",
            "osx": "dylib"
        }
        return os.path.expanduser(os.path.join("~", ".julia", "LanguageServer.{}".format(file_extension[sublime.platform()])))

    def validate(self, text):
        return os.path.exists(os.path.dirname(text))


class JuliaActivateEnvironmentCommand(LspTextCommand):
    def is_enabled(self):
        return self.view.match_selector(0, "source.julia") and self.has_client_with_capability(None)

    def run(self, edit, env_path):
        if env_path[-1] in {"/", "\\"}:
            env_path = env_path[0:-1]

        # send julia/activateenvironment notification
        client = self.client_with_capability(None)
        client.send_notification(Notification("julia/activateenvironment", env_path))

        # update settings
        update_starting_command(env_path)

        # update status bar
        settings = sublime.load_settings(SETTINGS_FILE)
        if settings.get("show_environment_status"):
            env_name = os.path.basename(env_path)
            update_environment_status(self.view.window(), env_name)

    def input(self, args):
        if "env_path" not in args:
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
        return self.view.match_selector(0, "source.julia") and self.has_client_with_capability(None)

    def run(self, edit):
        # send julia/getCurrentBlockRange request
        params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
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
        if not self.has_client_with_capability(None):
            return False
        # cursor must not be at end of file
        if self.view.sel()[0].b == self.view.size():
            return False
        return True

    def run(self, edit):
        window = self.view.window()
        sel = self.view.sel()[0]
        # ensure that Terminus output panel for Julia REPL is available
        repl_ready = ensure_terminus_repl(window)
        if sel.empty():
            # send julia/getCurrentBlockRange request
            params = versioned_text_document_position_params(self.view, self.view.sel()[0].b)
            client = self.client_with_capability(None)
            client.send_request(Request("julia/getCurrentBlockRange", params), self.handle_response)
        else:
            code_block = self.view.substr(sel)
            if repl_ready:
                send_terminus_repl(self.view.window(), code_block)
            else:
                sublime.set_timeout(lambda: send_terminus_repl(self.view.window(), code_block), 5)

    def handle_response(self, response):
        a = point_to_offset(Point.from_lsp(response[0]), self.view)
        b = point_to_offset(Point.from_lsp(response[1]), self.view)
        c = point_to_offset(Point.from_lsp(response[2]), self.view)
        code_block = self.view.substr(sublime.Region(a, b))
        # move cursor to next code block
        self.view.sel().clear()
        self.view.run_command("lsp_selection_add", {"regions": [(c, c)]})
        self.view.show_at_center(c)
        send_terminus_repl(self.view.window(), code_block)


class JuliaRunCodeCellCommand(LspTextCommand):
    def is_enabled(self):
        # must be Julia file
        if not self.view.match_selector(0, "source.julia"):
            return False
        # Terminus package must be installed
        if not importlib.find_loader("Terminus"):
            return False
        # Language Server must be ready
        if not self.has_client_with_capability(None):
            return False
        # cursor must not be at end of file
        if self.view.sel()[0].b == self.view.size():
            return False
        return True

    def run(self, edit):
        window = self.view.window()
        sel = self.view.sel()[0]
        # ensure that Terminus output panel for Julia REPL is available
        repl_ready = ensure_terminus_repl(window)
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
                # remove empty and commented lines
                if code_line and not code_line.lstrip().startswith("#"):
                    code_block += code_line + "\n"
            # select and scroll to next cell
            next_cell = line_end + 1
            while next_cell < line_count:
                if not self.view.substr(self.view.line(self.view.text_point(next_cell, 0))).startswith("#"):
                    break
                next_cell += 1
            c = self.view.text_point(next_cell, 0)
            self.view.sel().clear()
            self.view.run_command("lsp_selection_add", {"regions": [(c, c)]})
            self.view.show_at_center(c)
        else:
            code_block = self.view.substr(sel)
        if repl_ready:
            send_terminus_repl(self.view.window(), code_block)
        else:
            sublime.set_timeout(lambda: send_terminus_repl(self.view.window(), code_block), 5)


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


class JuliaOpenReplCommand(sublime_plugin.WindowCommand):
    def is_enabled(self):
        return importlib.find_loader("Terminus") is not None

    def run(self):
        repl_view = self.window.find_output_panel(JULIA_REPL_NAME)
        if repl_view:
            self.window.run_command("show_panel", {"panel": "output.{}".format(JULIA_REPL_NAME)})
            self.window.focus_view(repl_view)
        else:
            start_terminus_repl(self.window, True)


class JuliaExecuteCommand(LspExecuteCommand):
    def is_enabled(self):
        return self.view.match_selector(0, "source.julia") and self.has_client_with_capability(None)
