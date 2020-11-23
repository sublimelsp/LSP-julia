# LSP-julia

[![License](https://img.shields.io/github/license/sublimelsp/LSP-julia)](https://github.com/sublimelsp/LSP-julia/blob/master/LICENSE)

A plugin for the [LSP](https://packagecontrol.io/packages/LSP) client in Sublime Text with support for the [Julia language server](https://github.com/julia-vscode/LanguageServer.jl).

**Warning: This is an experimental branch for ST 4, but it is not tested yet and might not work**

## Installation

* [Julia](https://julialang.org/) must be installed on your machine. If the `julia` executable is not in your PATH, you will need to specify the full path to it in your LSP-julia settings.
* Install the [Julia](https://packagecontrol.io/packages/Julia) package from Package Control for syntax highlighting.
* Install the [LSP](https://packagecontrol.io/packages/LSP) package from Package Control.
* Clone the `st4` branch of this LSP-julia repository into your Packages directory. Ensure that the folder is named `LSP-julia`.
* Optionally install the [Terminus](https://packagecontrol.io/packages/Terminus) package from Package Control for a better Julia REPL integration and additional features (recommended).
* After opening a Julia file, wait for the language server to download and precompile. The precompilation process can take several minutes, but it only needs to be done again if there is a new release of LanguageServer.jl or if you update or switch to a different Julia version.

## Features

Most of the standard LSP features like autocompletion, documentation on hover, or goto definition are supported by the Julia language server.
Some LSP features like diagnostics require to open a folder in Sublime Text, and will not work in single file mode.

LSP-julia provides additional commands which are available from the command palette:

| Command label | Key binding | Description |
| ------------- | ----------- | ----------- |
| LSP-julia: Change Environment | none | Choose the Julia project environment you are working in. The packages from this environment are used by the language server to provide autocomplete suggestions and for diagnostics/linting. Dependent on the number of packages, it might take a while for the server to do a package indexing process in the background, after switching to a different environment. |
| LSP-julia: Open Julia REPL<sup>1</sup> | none | Open a Julia REPL, started in the directory of the active file, or focus if already running. |
| LSP-julia: Select Code Block | none | Select the function or code block at the current cursor position. For multiple active cursors, only the topmost cursor position is taken into account. |
| LSP-julia: Run Code Block<sup>1</sup> | <kbd>Alt</kbd>+<kbd>Enter</kbd> | If text is selected, run it in a Julia REPL. Otherwise, run the code block containing the current cursor position and move curser to the next block. |
| LSP-julia: Run Code Cell<sup>1</sup> | <kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>Enter</kbd> | If text is selected, run it in a Julia REPL. Otherwise, run the code cell containing the current cursor position and move curser to the next cell. Code cells are signalized with a specially formatted comment at the start of a line: `##`, `#%%` or `# %%`. |

Commands marked with a <sup>1</sup> are only available if you have the Terminus package installed.

To add or adjust key bindings for the commands, edit the `.sublime-keymap` file for your OS in your `Packages/User` folder.
For an example refer to the [Default.sublime-keymap](Default.sublime-keymap) file in this repository, and for the command names from this package see [LSP-julia.sublime-commands](LSP-julia.sublime-commands).
