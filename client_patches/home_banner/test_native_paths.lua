-- Exercise the exact global/UnitySupport helpers and home rendering bytecode.
local toolsPath, configPath, globalPath, supportPath = arg[1], arg[2], arg[3], arg[4]
local patchPath, wrappedPath, refreshPath, bannerPath = arg[5], arg[6], arg[7], arg[8]
local withdrawnPath = arg[9]
assert(toolsPath and configPath and globalPath and supportPath and patchPath)
dofile(globalPath)
dofile(supportPath)
MainAdvinfoCfg = dofile(configPath)
local active = {}
ActivityData = {
    GetActivityData = function(_, id)
        if active[id] then return { IsActivitying = function() return true end } end
    end
}
IsConditionAchieved = function() return false end
TimeMgr = { GetInstance = function()
    return {
        GetServerTime = function() return 1793668800 end,
        parseTimeFromConfig = function(_, value)
            return os.time({year=value[1][1], month=value[1][2], day=value[1][3],
                hour=value[2][1], min=value[2][2], sec=value[2][3]})
        end
    }
end }
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
    if wrappedPath and wrappedPath ~= "-" then
        ActivityTools = dofile(wrappedPath)
    else
        ActivityTools = dofile(toolsPath)
        install(ActivityTools)
    end
end

check("native sprite helper returns a path rather than a Unity object", function()
    assert(type(getSpriteWithoutAtlas("I18NImg/Banner/activity46")) == "string")
    local ok, err = pcall(isNil, getSpriteWithoutAtlas("I18NImg/Banner/activity46"))
    assert(not ok and tostring(err):find("Equals", 1, true))
end)
check("historical draw path resolves to available artwork without object checks", function()
    setup({3700302})
    local entries = ActivityTools.GetCurrentAdvInfo()
    assert(#entries == 1 and entries[1].id == 40501)
    assert(entries[1].image == "I18NImg/Banner/activity46")
end)
check("native current artwork and ordering are preserved", function()
    setup({3700302,4200301})
    local entries = ActivityTools.GetCurrentAdvInfo()
    assert(#entries == 2 and entries[1].id == 40501 and entries[2].id == 50101)
    assert(entries[2].image == "I18NImg/Banner/activity_1077")
end)
check("missing non-draw paths do not produce an empty carousel item", function()
    setup({3734001})
    assert(#ActivityTools.GetCurrentAdvInfo() == 0)
end)
check("historical banner still opens the original draw pool", function()
    setup({3700302})
    local clicked
    JumpTools = { JumpToPage2 = function(value) clicked = value end }
    ActivityTools.ClickAdv(ActivityTools.GetCurrentAdvInfo()[1])
    assert(clicked[1] == 404 and clicked[2] == 4050301)
end)

if refreshPath and bannerPath then
    local function vector(x, y, z) return {x=x, y=y, z=z} end
    Vector3 = setmetatable({}, { __call = function(_, x, y, z) return vector(x,y,z) end })
    class = function() return {} end
    ReduxView = {}
    local Banner = dofile(bannerPath)
    MainHomeView = {}
    partialClass = function(name) return assert(_G[name]) end
    -- Native RefreshUI adds methods to MainHomeView and needs no game instance.
    dofile(refreshPath)
    SetActive = function(go, value) go.active = value end
    local function home()
        local barTransform = { SetSiblingIndex = function() end }
        local banners = setmetatable({
            grid_ = { transform = {} }, item1_ = {}, item2_ = {}, item3_ = {},
            bar_ = { transform = barTransform }
        }, { __index = Banner })
        -- Unity bar creation and timers are unrelated to sprite path assignment.
        banners.InitBannerBar = function() end
        banners.StartTimer = function() end
        return setmetatable({btnActivityGo_ = {}, bannerView_ = banners}, { __index = MainHomeView })
    end
    check("native home and BannerView render historical paths without exceptions", function()
        setup({3700302,4200301})
        local view = home()
        view:RefreshBanner()
        assert(view.btnActivityGo_.active == true)
        assert(view.bannerView_.dataNum_ == 2)
        assert(view.bannerView_.item2_.sprite == "I18NImg/Banner/activity46")
        assert(view.bannerView_.item1_.sprite == "I18NImg/Banner/activity_1077")
        assert(view.bannerView_.item3_.sprite == "I18NImg/Banner/activity_1077")
    end)
    check("native home hides the carousel when no paths are available", function()
        setup({3734001})
        local view = home()
        view:RefreshBanner()
        assert(view.btnActivityGo_.active == false)
    end)
    if withdrawnPath then
        check("withdrawn wrapper reproduces the native home initialization error", function()
            setup({3700302})
            ActivityTools = dofile(withdrawnPath)
            local view = home()
            local ok, err = pcall(view.RefreshBanner, view)
            assert(not ok and tostring(err):find("Equals", 1, true))
        end)
    end
end
print('{"passed":' .. passed .. ',"failed":0}')
