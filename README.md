# LSP-julia

[![License](https://img.shields.io/github/license/jwortmann/LSP-julia)](https://github.com/jwortmann/LSP-julia/blob/master/LICENSE)

A plugin for the [LSP](https://packagecontrol.io/packages/LSP) client in Sublime Text with support for the [LanguageServer.jl](https://github.com/julia-vscode/LanguageServer.jl) Julia language server.

IMPORTANT: This plugin is in an early stage and under development, so it might not work flawlessly yet and there could be some breaking changes in the future.

## Installation

* Install [LanguageServer.jl](https://github.com/julia-vscode/LanguageServer.jl) from the Julia REPL (ensure to use version v3.2.0 or higher!):

```
julia> ]
pgk> add LanguageServer
```

* Install the [Julia](https://packagecontrol.io/packages/Julia) package from Package Control for syntax highlighting
* Install the [LSP](https://packagecontrol.io/packages/LSP) client from Package Control
* Clone this LSP-julia repository into your Packages directory.
  I will probably wait until there is a public release of ST4 and then update this plugin to use the new LSP API, before adding it to the default Package Control channel, so that it won't suddenly break for ST3 users.
* Optionally install the [Terminus](https://packagecontrol.io/packages/Terminus) package from Package Control for a better Julia REPL integration and additional features.

## Features

Most of the standard LSP features like autocompletion, documentation on hover, or goto definition are supported by the Julia language server.

LSP-julia provides additional commands which are available from the command palette:

| Command label | Key binding | Description |
| ------------- | ----------- | ----------- |
| LSP-julia: Change Environment | none | Choose the Julia project environment you are working in. Its packages are used by the language server to provide autocomplete suggestions. The server will take a while for indexing packages after running this command. |
| LSP-julia: Select Code Block | none | Select the function or code block at the current cursor position. For multiple active cursors, only the topmost cursor position is taken into account. |
| LSP-julia: Run Code Block | <kbd>Alt</kbd>+<kbd>Enter</kbd> | Run the function or code block at the current cursor position in a Julia REPL. This command is only available if you have the Terminus package installed. |
| LSP-julia: Expand Inline Function | none | Replace an inline (assignment form) function with the traditional function declaration syntax. Might crash the server if not run with the curser located inside an inline function. |

To add or adjust key bindings for the commands, edit the `.sublime-keymap` file for your OS in your `Packages/User` folder.
For an example refer to the [Default.sublime-keymap](Default.sublime-keymap) file in this repository, and for the command names see [LSP-julia.sublime-commands](LSP-julia.sublime-commands).

## Miscellaneous

The startup time for the Julia language server can be noticeably reduced by precompiling the LanguageServer.jl package via [PackageCompiler.jl](https://github.com/JuliaLang/PackageCompiler.jl) into a system image file:
```
julia> using PackageCompiler
pkg> activate .
pkg> add LanguageServer
julia> create_sysimage(:LanguageServer; sysimage_path="LanguageServer.so")
```
Then add the starting option `"--sysimage", "path/to/LanguageServer.so"` into `"command"` in your user settings file.

## Known issues and workarounds

* LSP leaves orphaned Julia processes for the language server, see [LSP#410](https://github.com/sublimelsp/LSP/issues/410) and [LSP#869](https://github.com/sublimelsp/LSP/issues/869).
  There is a workaround for Linux/macOS to start the server via a [Bash script](https://github.com/julia-vscode/LanguageServer.jl/blob/master/contrib/languageserver.sh) which periodically checks for and kills orphaned language server processes, but that doesn't allow to specify a path to the active Julia environment in its starting arguments.
