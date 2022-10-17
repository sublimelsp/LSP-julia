import JSON
# import Test

function run_testitem(params)
    return Dict("status" => "passed", "message" => nothing, "duration" => 1.23)
    # return Dict("status" => "errored", "message" => "error message")
end

result = run_testitem(JSON.parse(ARGS[1]))
result_json = JSON.json(result)
print(result_json)
