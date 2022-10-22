import Pkg  # stdlib
import Test # stdlib

try
    import JSON
    import Suppressor
    import TestEnv
    import URIParser
catch
    Pkg.instantiate(; io=devnull)
    import JSON
    import Suppressor
    import TestEnv
    import URIParser
end


TestserverRunTestitemRequestParamsReturn(status, message, duration) = Dict("status"=>status, "message"=>message, "duration"=>duration)
TestMessage(message, location) = Dict("message"=>message, "location"=>location)
Location(uri, range) = Dict("uri"=>uri, "range"=>range)
Range(start, stop) = Dict("start"=>start, "end"=>stop)
Position(line, character) = Dict("line"=>line, "character"=>character)

struct TestserverRunTestitemRequestParams
    uri::String
    name::String
    packageName::String
    useDefaultUsings::Bool
    line::Int
    column::Int
    code::String
end

#==========================================================================================#
# Adjusted functions extracted from VSCodeTestServer.jl
# Keep in sync with https://github.com/julia-vscode/julia-vscode/blob/main/scripts/packages/VSCodeTestServer/src/VSCodeTestServer.jl

function uri2filepath(uri::AbstractString)
    parsed_uri = try
        URIParser.URI(uri)
    catch
        return nothing
    end

    if parsed_uri.scheme !== "file"
        return nothing
    end

    path_unescaped = URIParser.unescape(parsed_uri.path)
    host_unescaped = URIParser.unescape(parsed_uri.host)

    value = ""

    if host_unescaped != "" && length(path_unescaped) > 1
        # unc path: file://shares/c$/far/boo
        value = "//$host_unescaped$path_unescaped"
    elseif length(path_unescaped) >= 3 &&
           path_unescaped[1] == '/' &&
           isascii(path_unescaped[2]) && isletter(path_unescaped[2]) &&
           path_unescaped[3] == ':'
        # windows drive letter: file:///c:/far/boo
        value = lowercase(path_unescaped[2]) * path_unescaped[3:end]
    else
        # other path
        value = path_unescaped
    end

    if Sys.iswindows()
        value = replace(value, '/' => '\\')
    end

    value = normpath(value)

    return value
end

function filepath2uri(file::String)
    isabspath(file) || error("Relative path `$file` is not valid.")
    if Sys.iswindows()
        file = normpath(file)
        file = replace(file, "\\" => "/")
        file = URIParser.escape(file)
        file = replace(file, "%2F" => "/")
        if startswith(file, "//")
            # UNC path \\foo\bar\foobar
            return string("file://", file[3:end])
        else
            # windows drive letter path
            return string("file:///", file)
        end
    else
        file = normpath(file)
        file = URIParser.escape(file)
        file = replace(file, "%2F" => "/")
        return string("file://", file)
    end
end

function withpath(f, path)
    tls = task_local_storage()
    hassource = haskey(tls, :SOURCE_PATH)
    hassource && (path′ = tls[:SOURCE_PATH])
    tls[:SOURCE_PATH] = path
    try
        return f()
    finally
        hassource ? (tls[:SOURCE_PATH] = path′) : delete!(tls, :SOURCE_PATH)
    end
end

function format_error_message(err, bt)
    try
        return Base.invokelatest(sprint, Base.display_error, err, bt)
    catch err
        # TODO We could probably try to output an even better error message here that
        # takes into account `err`. And in the callsites we should probably also
        # handle this better.
        return "Error while trying to format an error message"
    end
end

function run_testitem(params::TestserverRunTestitemRequestParams)
    mod = Core.eval(Main, :(module $(gensym()) end))

    if params.useDefaultUsings
        try
            Core.eval(mod, :(using Test))
        catch
            return TestserverRunTestitemRequestParamsReturn(
                "errored",
                [
                    TestMessage(
                        "Unable to load the `Test` package. Please ensure that `Test` is listed as a test dependency in the Project.toml for the package.",
                        Location(
                            params.uri,
                            Range(Position(params.line, 0), Position(params.line, 0))
                        )
                    )
                ],
                nothing
            )
        end

        if params.packageName!=""
            try
                Core.eval(mod, :(using $(Symbol(params.packageName))))
            catch err
                bt = catch_backtrace()
                error_message = format_error_message(err, bt)

                return TestserverRunTestitemRequestParamsReturn(
                    "errored",
                    [
                        TestMessage(
                            error_message,
                            Location(
                                params.uri,
                                Range(Position(params.line, 0), Position(params.line, 0))
                            )
                        )
                    ],
                    nothing
                )
            end
        end
    end

    filepath = uri2filepath(params.uri)

    code = string('\n'^params.line, ' '^params.column, params.code)

    ts = Test.DefaultTestSet("$filepath:$(params.name)")

    Test.push_testset(ts)

    elapsed_time = UInt64(0)

    t0 = time_ns()
    try
        withpath(filepath) do
            Base.invokelatest(include_string, mod, code, filepath)
            elapsed_time = (time_ns() - t0) / 1e6 # Convert to milliseconds
        end
    catch err
        elapsed_time = (time_ns() - t0) / 1e6 # Convert to milliseconds

        Test.pop_testset()

        bt = catch_backtrace()
        st = stacktrace(bt)

        error_message = format_error_message(err, bt)

        if err isa LoadError
            error_filepath = err.file
            error_line = err.line
        else
            error_filepath =  string(st[1].file)
            error_line = st[1].line
        end

        return TestserverRunTestitemRequestParamsReturn(
            "errored",
            [
                TestMessage(
                    error_message,
                    Location(
                        isabspath(error_filepath) ? filepath2uri(error_filepath) : "",
                        Range(Position(max(0, error_line - 1), 0), Position(max(0, error_line - 1), 0))
                    )
                )
            ],
            elapsed_time
        )
    end

    ts = Test.pop_testset()

    try
        Test.finish(ts)

        return TestserverRunTestitemRequestParamsReturn("passed", nothing, elapsed_time)
    catch err
        if err isa Test.TestSetException
            failed_tests = Test.filter_errors(ts)

            return TestserverRunTestitemRequestParamsReturn(
                "failed",
                [TestMessage(sprint(Base.show, i), Location(filepath2uri(string(i.source.file)), Range(Position(i.source.line - 1, 0), Position(i.source.line - 1, 0)))) for i in failed_tests],
                elapsed_time
            )
        else
            rethrow(err)
        end
    end
end

#==========================================================================================#

params_dict = JSON.parse(ARGS[1])
package_name = params_dict["packageName"]
project_path = params_dict["project_path"]
package_path = params_dict["package_path"]

params = TestserverRunTestitemRequestParams(
    params_dict["uri"],
    params_dict["name"],
    package_name,
    params_dict["useDefaultUsings"],
    params_dict["line"],
    params_dict["column"],
    params_dict["code"]
)

result = nothing

Suppressor.@suppress begin
    if project_path==""
        Pkg.activate(temp=true)

        Pkg.develop(path=package_path)

        TestEnv.activate(package_name) do
            global result = run_testitem(params)
        end
    else
        Pkg.activate(project_path)

        if package_name!=""
            TestEnv.activate(package_name) do
                global result = run_testitem(params)
            end
        else
            global result = run_testitem(params)
        end
    end
end

print(JSON.json(result))
