-- Run against original ActivityTools and configuration bytecode from the client.
local originalPath, configPath, patchPath = arg[1], arg[2], arg[3]
local wrappedPath = arg[4]
assert(originalPath and configPath and patchPath)
MainAdvinfoCfg = dofile(configPath)
local active = {}
local available = {
    ["I18NImg/Banner/activity46"] = true,
    ["I18NImg/Banner/activity_1077"] = true,
    ["I18NImg/Banner/activity_1166"] = true
}
local fakeNull = {}
isNil = function(sprite)
    return sprite == nil or sprite == fakeNull
end
getSpriteWithoutAtlas = function(path)
    if available[path] == "null" then return fakeNull end
    if available[path] then return { path = path } end
end
IsConditionAchieved = function() return false end
ActivityData = {
    GetActivityData = function(_, id)
        if active[id] then return { IsActivitying = function() return true end } end
    end
}
local clock = {
    GetServerTime = function() return 1793668800 end,
    parseTimeFromConfig = function(_, value)
        return os.time({ year = value[1][1], month = value[1][2], day = value[1][3],
            hour = value[2][1], min = value[2][2], sec = value[2][3] })
    end
}
TimeMgr = { GetInstance = function() return clock end }
ActivityTools = dofile(originalPath)
local rawTools = ActivityTools.GetCurrentAdvInfo
local install = dofile(patchPath)
local passed = 0
local function check(name, callback)
    callback()
    passed = passed + 1
    print("ok " .. name)
end
local function setup(ids)
    active = {}
    for _, id in ipairs(ids) do active[id] = true end
    if wrappedPath then
        ActivityTools = dofile(wrappedPath)
    else
        ActivityTools.GetCurrentAdvInfo = rawTools
        install(ActivityTools)
    end
end

check("original historical draw banner reproduces missing image", function()
    active = { [3700302] = true }
    local banners = rawTools()
    assert(#banners == 1 and banners[1].id == 40501 and banners[1].image == nil)
end)
check("missing draw image uses an existing native draw banner", function()
    setup({3700302})
    local banners = ActivityTools.GetCurrentAdvInfo()
    assert(#banners == 1 and banners[1].id == 40501)
    assert(banners[1].image.path == "I18NImg/Banner/activity46")
end)
check("historical draw click retains the selected pool", function()
    local clicked
    JumpTools = { JumpToPage2 = function(parameter) clicked = parameter end }
    ActivityTools.ClickAdv(ActivityTools.GetCurrentAdvInfo()[1])
    assert(clicked[1] == 404 and clicked[2] == 4050301)
end)
check("available current draw artwork is retained", function()
    setup({4200301})
    local banners = ActivityTools.GetCurrentAdvInfo()
    assert(#banners == 1 and banners[1].id == 50101)
    assert(banners[1].image.path == "I18NImg/Banner/activity_1077")
end)
check("mixed banners retain native order and contain no empty image", function()
    setup({3700302,4200301})
    local banners = ActivityTools.GetCurrentAdvInfo()
    assert(#banners == 2 and banners[1].id == 40501 and banners[2].id == 50101)
    for _, banner in ipairs(banners) do assert(not isNil(banner.image)) end
end)
check("missing non-draw artwork does not create a blank carousel item", function()
    assert(MainAdvinfoCfg[40503].parameter[1] ~= 404)
    setup({3734001})
    assert(#ActivityTools.GetCurrentAdvInfo() == 0)
end)
check("Unity null sprites are treated as unavailable", function()
    available["I18NImg/Banner/activity_1044"] = "null"
    setup({3700302})
    assert(ActivityTools.GetCurrentAdvInfo()[1].image.path == "I18NImg/Banner/activity46")
end)
check("newly available historical artwork replaces the fallback", function()
    available["I18NImg/Banner/activity_1044"] = true
    local banners = ActivityTools.GetCurrentAdvInfo()
    assert(banners[1].image.path == "I18NImg/Banner/activity_1044")
    available["I18NImg/Banner/activity_1044"] = nil
end)
check("missing fallback cannot be inserted as another blank item", function()
    available["I18NImg/Banner/activity46"] = nil
    assert(#ActivityTools.GetCurrentAdvInfo() == 0)
end)
print('{"passed":' .. passed .. ',"failed":0}')
