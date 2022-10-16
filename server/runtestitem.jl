import Test

function run_testitem(uri::String, name::String, packageName::String, useDefaultUsings::Bool, line::Int, column::Int, code::String)
    nothing
end

uri = ARGS[1]
name = ARGS[2]
packageName = ARGS[3]
useDefaultUsings = ARGS[4] === "True"
line = parse(Int, ARGS[5])
column = parse(Int, ARGS[6])
code = ARGS[7]

run_testitem(uri, name, packageName, useDefaultUsings, line, column, code)
