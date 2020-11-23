using LanguageServer, SymbolServer, LanguageServer.JSON
msg = Dict("jsonrpc" => "2.0", "id" => 1, "method" => "exit", "params" => Dict()) |> JSON.json
input = IOBuffer("Content-Length: $(length(msg))\n\n$msg")
server = LanguageServerInstance(input, stdout, dirname(Base.load_path_expand("@v#.#")), get(ENV, "JULIA_DEPOT_PATH", ""))
run(server)
