# LSP-julia

[![License](https://img.shields.io/github/license/sublimelsp/LSP-julia)](https://github.com/sublimelsp/LSP-julia/blob/master/LICENSE)

A plugin for the LSP client in Sublime Text with support for the [Julia language server](https://github.com/julia-vscode/LanguageServer.jl).


## Requirements & Installation

The following should be installed:

* [Julia](https://julialang.org/) 1.11, 1.12, or 1.13
* The [Julia package](https://packages.sublimetext.io/packages/Julia/) from Package Control
* The [LSP](https://packages.sublimetext.io/packages/LSP/) and [LSP-julia](https://packages.sublimetext.io/packages/LSP-julia/) packages from Package Control
* Optionally the [Terminus](https://packages.sublimetext.io/packages/Terminus/) package from Package Control for a basic Julia REPL integration and the ability to run code blocks with a key binding

> [!NOTE]
> If the `julia` executable is not in your PATH, you need to provide the full path to the executable in the LSP-julia package settings.

When a Julia file is opened for the first time after installing LSP-julia, the language server will automatically be installed with the Julia package manager. This can take a few minutes.


## Features

The Julia language server supports most of the standard LSP features like auto-completion, documentation on hover, goto definition, and diagnostics (linting).

> [!IMPORTANT]
> Most features require that a folder was opened in Sublime Text and don't work in single file mode.

LSP-julia provides additional commands which are available from the command palette:

| Command label | Key binding | Description |
| ------------- | ----------- | ----------- |
| LSP-julia: Change Current Environment | none | Select the Julia project environment you are working in. The packages from this environment are used by the language server to provide autocomplete suggestions and for diagnostics/linting. Dependent on the number of packages, it might take a while for the server to do a package indexing process in the background, after switching to a different environment. |
| LSP-julia: Documentation | none | Search the Julia documentation[^1] and open the results in a tab. You can also right-click on a word in a Julia file and select "Show Documentation" from the context menu to open the corresponding documentation page. |
| LSP-julia: Open Julia REPL in Panel[^2] | none | Open a Julia REPL, started in the directory of the active file, or focus if already running. |
| LSP-julia: Open Julia REPL in Tab[^2] | none | Same as above, but use a normal tab instead of the bottom panel for the REPL. |
| LSP-julia: Select Code Block | none | Select the function or code block at the current cursor position. For multiple active cursors, only the topmost cursor position is taken into account. |
| LSP-julia: Run Code Block[^2] | <kbd>Alt</kbd>+<kbd>Enter</kbd> | If text is selected, run it in a Julia REPL. Otherwise, run the code block containing the current cursor position and move curser to the next block. |
| LSP-julia: Run Code Cell[^2] | <kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>Enter</kbd> | If text is selected, run it in a Julia REPL. Otherwise, run the code cell containing the current cursor position and move curser to the next cell. Code cells are signalized with a specially formatted comment at the start of a line: `##`, `#%%` or `# %%`. |

[^1]: The documentation pages are dynamically generated from docstrings in Julia base, the standard library and in Julia packages, not from the official documentation on the Julia website.
[^2]: Only available if you have the Terminus package installed.

To add or adjust key bindings for the commands, run *Preferences: Key Bindings* from the command palette and modify the user file on the righthand side.
For an example refer to the [Default.sublime-keymap](Default.sublime-keymap) file in this repository, and for the command names from this package see [LSP-julia.sublime-commands](LSP-julia.sublime-commands).


## Troubleshooting

### I have deleted or cleaned up my `.julia` directory. Now the language server doesn't start anymore.

Delete the `LSP-julia` folder at the following location:
* on Windows: `%LocalAppData%/Sublime Text/Package Storage/LSP-julia`
* on Linux: `~/.cache/sublime-text/Package Storage/LSP-julia`
* on macOS: `~/Library/Application Support/Sublime Text/Package Storage/LSP-julia`

Then restart Sublime Text and open a Julia file to re-install the language server.


## Instructions for Maintainers

To update the language server, just modify the `rev` value in [server/Project.toml](https://github.com/sublimelsp/LSP-julia/blob/main/server/Project.toml) with a new commit SHA from [LanguageServer.jl](https://github.com/julia-vscode/LanguageServer.jl/commits/main/) and push the changes to the `main` branch.
This will trigger a GitHub workflow that resolves the server dependencies for all supported Julia versions, and automatically creates a pull request with the changes to the corresponding `Manifest.toml` files.
