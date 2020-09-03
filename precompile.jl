using Pkg
Pkg.activate(Base.ARGS[1])
Pkg.add(PackageSpec(name="PackageCompiler", version="1.2.1"))
Pkg.add(PackageSpec(name="LanguageServer", version="3.2.0"))
Pkg.add(PackageSpec(name="SymbolServer", version="5.1.0"))
using PackageCompiler
create_sysimage([:LanguageServer, :SymbolServer]; sysimage_path=Base.ARGS[2], precompile_execution_file=joinpath(Base.ARGS[1], "precompile_execution_file.jl"))
