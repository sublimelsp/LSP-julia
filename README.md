# LSP-julia

[![License](https://img.shields.io/github/license/jwortmann/LSP-julia)](https://github.com/jwortmann/LSP-julia/blob/master/LICENSE)

A plugin for the [LSP](https://packagecontrol.io/packages/LSP) client in Sublime Text 3 with support for the [LanguageServer.jl](https://github.com/julia-vscode/LanguageServer.jl) Julia language server.

IMPORTANT: This plugin is still under development, so it might not work flawlessly yet and there could be some breaking changes in the future.

## Installation

* Install the [Julia](https://packagecontrol.io/packages/Julia) package from Package Control for syntax highlighting
* Install the [LSP](https://packagecontrol.io/packages/LSP) client from Package Control
* Clone this LSP-julia repository into your Packages directory.
  I will probably wait until there is a public release of ST4 and then update this plugin to use the new LSP API, before adding it to the default Package Control channel, so that it won't suddenly break for ST3 users.
* Install [PackageCompiler.jl](https://github.com/JuliaLang/PackageCompiler.jl) from the Julia REPL into your default Julia environment and then run the command `LSP-julia: Precompile Language Server` from within Sublime Text.
  This will create a custom system image file for the language server, which will noticeably reduce its startup time.
  Alternatively you can install [LanguageServer.jl](https://github.com/julia-vscode/LanguageServer.jl) from the Julia REPL into your default Julia environment (ensure to use version v3.2.0 or higher!):

    ```
    julia> ]
    pgk> add LanguageServer
    ```

* Optionally install the [Terminus](https://packagecontrol.io/packages/Terminus) package from Package Control for a better Julia REPL integration and additional features.

## Features

Most of the standard LSP features like autocompletion, documentation on hover, or goto definition are supported by the Julia language server.

LSP-julia provides additional commands which are available from the command palette:

| Command label | Key binding | Description |
| ------------- | ----------- | ----------- |
| LSP-julia: Precompile Language Server | none | Allows to precompile the server, which will noticeably reduce its startup time. The precompilation process will take a few minutes, but this command has to be run only when a new version of the language server is released (and this LSP-julia package has been updated with the new version numbers). Ensure to have PackageCompiler.jl added to your default Julia environment, before running this command! |
| LSP-julia: Change Environment | none | Choose the Julia project environment you are working in. Its packages are used by the language server to provide autocomplete suggestions. The server will take a while for indexing packages after running this command. |
| LSP-julia: Open REPL | none | Open a Julia REPL, started in the directory of the active file, or focus if already running. This command is only available if you have the Terminus package installed. |
| LSP-julia: Select Code Block | none | Select the function or code block at the current cursor position. For multiple active cursors, only the topmost cursor position is taken into account. |
| LSP-julia: Run Code Block | <kbd>Alt</kbd>+<kbd>Enter</kbd> | If text is selected, run it in a Julia REPL. Otherwise, run the code block at the current cursor position and move curser to the next code block. This command is only available if you have the Terminus package installed. |
| LSP-julia: Expand Inline Function | none | Replace an inline (assignment form) function with the traditional function declaration syntax. Might crash the server if not run with the curser located inside an inline function. |

To add or adjust key bindings for the commands, edit the `.sublime-keymap` file for your OS in your `Packages/User` folder.
For an example refer to the [Default.sublime-keymap](Default.sublime-keymap) file in this repository, and for the command names see [LSP-julia.sublime-commands](LSP-julia.sublime-commands).

## Known issues and workarounds

* LSP leaves orphaned Julia processes for the language server on Sublime Text 3, see [LSP#410](https://github.com/sublimelsp/LSP/issues/410) and [LSP#869](https://github.com/sublimelsp/LSP/issues/869).
  There is a workaround for Linux/macOS to start the server via a [Bash script](https://github.com/julia-vscode/LanguageServer.jl/blob/master/contrib/languageserver.sh) which periodically checks for and kills orphaned language server processes, but that doesn't allow to specify a path to the active Julia environment in its starting arguments.
