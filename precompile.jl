using Pkg
using PackageCompiler
Pkg.activate(Base.ARGS[1])
Pkg.add(PackageSpec(name="LanguageServer", version="3.2.0"))
Pkg.add(PackageSpec(name="SymbolServer", version="5.1.0"))
create_sysimage([:LanguageServer, :SymbolServer]; sysimage_path=Base.ARGS[2])
