# LSP-julia

[![License](https://img.shields.io/github/license/sublimelsp/LSP-julia)](https://github.com/sublimelsp/LSP-julia/blob/master/LICENSE)

A plugin for the [LSP](https://packagecontrol.io/packages/LSP) client in Sublime Text 3 with support for the [Julia language server](https://github.com/julia-vscode/LanguageServer.jl).

IMPORTANT: This plugin only works on ST 3 right now, but it will be updated to the new LSP API for ST 4 and added to Package Control when there is a public release for ST 4.

## Installation

* Install the [Julia](https://packagecontrol.io/packages/Julia) package from Package Control for syntax highlighting
* Install the [LSP](https://packagecontrol.io/packages/LSP) package from Package Control
* Clone this LSP-julia repository into your Packages directory
* To install the language server, there are two options:
  1. Run the command `LSP-julia: Precompile Language Server` from the command palette.
     This will create a custom Julia system image file for the language server, which will noticeably reduce its startup time.
     The precompilation process will take a few minutes, but this has to be done only when you update or switch to a different Julia version, or when a new version of the language server is released (and this LSP-julia package has been updated with the new version number accordingly).
  2. Alternatively you can install the [LanguageServer.jl](https://github.com/julia-vscode/LanguageServer.jl) package from the Julia REPL into your default Julia environment (ensure to use version v3.2.0 or higher!):

    ```
    julia> ]
    pgk> add LanguageServer
    ```

* Optionally install the [Terminus](https://packagecontrol.io/packages/Terminus) package from Package Control for a better Julia REPL integration and additional features (recommended).

## Features

Most of the standard LSP features like autocompletion, documentation on hover, or goto definition are supported by the Julia language server.

LSP-julia provides additional commands which are available from the command palette:

| Command label | Key binding | Description |
| ------------- | ----------- | ----------- |
| LSP-julia: Precompile Language Server | none | Allows to precompile the server, which will noticeably reduce its startup time. |
| LSP-julia: Change Environment | none | Choose the Julia project environment you are working in. Its packages are used by the language server to provide autocomplete suggestions. The server will take a while for indexing packages after running this command. |
| LSP-julia: Open REPL<sup>1</sup> | none | Open a Julia REPL, started in the directory of the active file, or focus if already running. |
| LSP-julia: Select Code Block | none | Select the function or code block at the current cursor position. For multiple active cursors, only the topmost cursor position is taken into account. |
| LSP-julia: Run Code Block<sup>1</sup> | <kbd>Alt</kbd>+<kbd>Enter</kbd> | If text is selected, run it in a Julia REPL. Otherwise, run the code block containing the current cursor position and move curser to the next block. |
| LSP-julia: Run Code Cell<sup>1</sup> | <kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>Enter</kbd> | If text is selected, run it in a Julia REPL. Otherwise, run the code cell containing the current cursor position and move curser to the next cell. Code cells are signalized with a specially formatted comment at the start of a line: `##`. |
<!-- | LSP-julia: Expand Inline Function | none | Replace an inline (assignment form) function with the traditional function declaration syntax. Might crash the server if not run with the curser located inside an inline function. | -->

Commands marked with a <sup>1</sup> are only available if you have the Terminus package installed.

To add or adjust key bindings for the commands, edit the `.sublime-keymap` file for your OS in your `Packages/User` folder.
For an example refer to the [Default.sublime-keymap](Default.sublime-keymap) file in this repository, and for the command names see [LSP-julia.sublime-commands](LSP-julia.sublime-commands).

## Known issues and workarounds

* LSP leaves orphaned Julia processes for the language server on Sublime Text 3, see [LSP#410](https://github.com/sublimelsp/LSP/issues/410) and [LSP#869](https://github.com/sublimelsp/LSP/issues/869).
  There is a workaround for Linux/macOS to start the server via a [Bash script](https://github.com/julia-vscode/LanguageServer.jl/blob/master/contrib/languageserver.sh) which periodically checks for and kills orphaned language server processes, but that doesn't allow to specify a path to the active Julia environment in its starting arguments.
