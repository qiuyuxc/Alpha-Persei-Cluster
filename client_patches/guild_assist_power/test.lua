-- Run using the client's native guild classes, foreign template and calculator.
local native, patchPath, wrapped = arg[1], arg[2], arg[3]
assert(native and patchPath)
local function noop() end
function class(_, parent)
    local c = {super = parent}
    c.__index = c
    setmetatable(c, {__index = parent})
    function c.New(...)
        local value = setmetatable({}, c)
        if value.Ctor then value:Ctor(...) end
        return value
    end
    return c
end
singletonClass = class
ReduxView = {}
function import() return {} end
function cleanProtoTable(values) return values or {} end
HeroCfg = {[1039] = {id = 1039, unlock_star = 300, skills = {103901,103902,103903,103904,103905,103906}, race = 1}}
HeroStarCfg = {[300] = {star = 3}}
HeroConst = {HERO_ATTRIBUTE = {ATK = 1, ARM = 2, STA = 3, CRITICAL_RATE = 81}, HERO_DATA_TYPE = {FOREIGN = 3}}
HeroTrustRelationNet = {New = function() return {InitData = noop, GetRelationNetAttr = function() return {} end} end}
HeroTools = {
    GetHeroAttribute = function(hero) return {[1] = hero.level * 10, [2] = hero.level * 2, [3] = hero.level * 100, [81] = 50} end,
    AttributeAdd = function(_, left, right) return (left or 0) + right end,
    CalTransitionSkillAttribute = function() return {} end,
    GetModuleAttribute = function() return {} end,
    GetSkillAttribute = function() return {} end,
    CalFinalAttribute = noop,
    GetHeroSkillAddLevel = function() return 0 end,
    GetHeroWeaponAddLevel = function() return 0 end,
    GetIsSuitNumCut = function() return false end,
    CountTransitionTotalSkill = function() return 0 end,
    IsSpHero = function() return false end,
    GetHeadSprite = function(id) assert(id); return id end
}
EquipCfg = {[510001] = {pos = 1, suit = 1}}
EquipSuitCfg = {get_id_list_by_suit = {[1] = {1}}}
EquipStruct = {New = function() return {ParseServerData = function(self, values) for k,v in pairs(values) do self[k] = v end end} end}
EquipTools = {
    CalHeroVirtualEquipAttribute = function(items)
        local attack = 0
        for _, item in ipairs(items) do attack = attack + math.floor(item.exp / 100) end
        return {[1] = attack}
    end,
    CountHeroTotalSkill = function(items)
        local levels = 0
        for _, item in ipairs(items) do
            for _, slot in pairs(item.enchant_slot_list or {}) do
                for _, effect in ipairs(slot.effect_list or {}) do levels = levels + effect.level end
            end
        end
        return {levels}
    end,
    GetSuitNeedNum = function() return 3 end
}
WeaponTools = {ExpToLv = function(exp) return math.floor(exp / 100) + 1 end,
    CalWeaponAttribute = function(_, weapon) return {[1] = weapon.level * 3} end}
WeaponServantCfg = {[251001] = {type = 1, starlevel = 5, effect = {0}}}
DormTools = {GetDormAdditionByHeroID = function() return {attribute = {}} end}
PlayerData = {GetPlayerInfo = function() return {userID = 5000} end}
Debug = {LogError = error}
GetI18NText = function(value) return value end
GetTips = function(value) return value end
GUILD_ASSIST_BRIEF_UPDATE = "guild_assist_brief_update"
BaseHeroDataTemplate = dofile(native .. "/BaseHeroDataTemplate.lua.luac")
ForeignHeroDataTemplate = dofile(native .. "/ForeignHeroDataTemplate.lua.luac")
dofile(native .. "/BattlePowerTools.lua.luac")
local install = dofile(patchPath)
local function asset(name)
    if wrapped then return dofile(wrapped .. "/" .. name .. ".luac") end
    return install(name, dofile(native .. "/" .. name .. ".luac"))
end
GuildData = asset("GuildData.lua")
local Head = asset("GuildBossAssistantHeadItemView.lua")
local Proxy = asset("GuildBossSelectHeroProxy.lua")
local requests, listeners, events
local function reset()
    requests, listeners, events = {}, {}, 0
    manager = {net = {SendWithLoadingNew = function(_, cmd, body, ack, callback)
        assert(cmd == 32020 and ack == 32021 and body.type == 2)
        requests[#requests + 1] = {owner = body.user_id, callback = callback}
    end}, notify = {Invoke = function(_, event)
        assert(event == GUILD_ASSIST_BRIEF_UPDATE)
        events = events + 1
        for _, listener in ipairs(listeners) do listener() end
    end}}
    GuildData.bossData_ = {assist_info_list = {}}
    GuildData.guildInfo_ = {id = 7001}
end
local function brief(owner, power)
    return {member_id = owner, member_post = 3, assist_hero_id = 1039, assist_hero_skin = 1039,
        fight_capacity = power or 0, hero_rare = 300, hero_level = 47, nick = "owner" .. owner,
        hero_oath_display = {hero_id = 1039, oath = false}}
end
local function record(level, equipment)
    local skills = {}
    for i = 1, 6 do skills[i] = {skill_id = 103900 + i, skill_level = 5} end
    return {hero_base_info = {id = 1039, level = level, exp = 0, star = 300, skill = skills,
        skill_intensify_attribute_list = {}, using_astrolabe = {10390011,10390012,10390013},
        unlock_astrolabe = {}, break_level = 0, weapon = {exp = 1000, breakthrough = 0},
        servant = {id = 251001, stage = 2}, using_skin = 1039, battle_using_skin = 1039,
        exclusive_skill_list = {}, weapon_module_level = 0}, equip_list = equipment or {},
        using_hero_chip = {}, dorm_level = 0, trust = {level = 0, exp = 0, mood = 1, relation = {}}}
end
local function reply(index, level, equipment)
    local req = requests[index]
    req.callback({result = 0, user_id = req.owner, hero_list = {record(level, equipment)}})
end
local function card(entry)
    local object = {data_ = entry, headIcon_ = {SetNativeSize = noop}, nickLabel_ = {},
        levelText_ = {}, fightPowerLabel_ = {}, clubMemberLabel_ = {},
        RegistEventListener = function(_, event, callback)
            assert(event == GUILD_ASSIST_BRIEF_UPDATE); listeners[#listeners + 1] = callback
        end}
    for _, key in ipairs({"campController_","gradeController_","positionController_","loveCon_"}) do
        object[key] = {SetSelectedState = noop}
    end
    setmetatable(object, {__index = Head})
    object:OnEnter()
    object:SetData(entry)
    return object
end
local passed = 0
local function check(name, run)
    reset(); run(); passed = passed + 1; print("ok " .. name)
end
check("native detail calculator produces the expected score", function()
    local hero = ForeignHeroDataTemplate.New(1039); hero:Init(record(47))
    assert(calcBattlePower(hero, hero:GetServantInfo(), hero.equip_list) == 3887)
end)
check("brief zero updates the native card from current owner data", function()
    GuildData:InitAssistInfoListFromServer({brief(3001)})
    local entry = GuildData:GetAssistantBriefList()[1]
    local view = card(entry); assert(view.fightPowerLabel_.text == 0)
    reply(1,47); assert(entry.fight_capacity == 3887 and view.fightPowerLabel_.text == 3887)
    assert(entry.nick == "owner3001" and entry.hero_level == 47)
end)
check("selected team reads current power despite a saved zero", function()
    GuildData:InitAssistInfoListFromServer({brief(3001)})
    reply(1,47)
    local proxy = setmetatable({reserveHeroInfoList = {{type = 2, info = brief(3001)}}}, {__index = Proxy})
    assert(proxy:CustomGetHeroPower(1) == 3887)
end)
check("refresh reflects owner leveling", function()
    GuildData:InitAssistInfoListFromServer({brief(3001)}); reply(1,47)
    GuildData:InitAssistInfoListFromServer({brief(3001)}); reply(2,49)
    assert(GuildData:GetAssistantBriefList()[1].fight_capacity == 3940)
end)
check("owner equipment and enchantments affect the same native calculation", function()
    GuildData:InitAssistInfoListFromServer({brief(3001)})
    reply(1,47,{{equip_id = 510001, exp = 1000, now_break_level = 0, race = 0,
        enchant_slot_list = {{id = 1, effect_list = {{id = 1001, level = 2}}}}}})
    assert(GuildData:GetAssistantBriefList()[1].fight_capacity == 4153)
end)
check("two owners of the same hero keep separate scores", function()
    GuildData:InitAssistInfoListFromServer({brief(3001),brief(3002)})
    assert(#requests == 2)
    for i = 1, 2 do reply(i, requests[i].owner == 3001 and 47 or 12) end
    for _,entry in ipairs(GuildData:GetAssistantBriefList()) do
        assert(entry.fight_capacity == (entry.member_id == 3001 and 3887 or 2956))
    end
end)
check("outdated refresh replies cannot overwrite newer scores", function()
    GuildData:InitAssistInfoListFromServer({brief(3001)})
    GuildData:InitAssistInfoListFromServer({brief(3001)})
    reply(2,49); reply(1,12)
    assert(GuildData:GetAssistantBriefList()[1].fight_capacity == 3940)
end)
check("leaving the guild ignores in-flight replies", function()
    GuildData:InitAssistInfoListFromServer({brief(3001)})
    GuildData.guildInfo_ = {id = 0}; reply(1,47)
    assert(GuildData:GetAssistantBriefList()[1].fight_capacity == 0)
end)
check("wrong owner replies cannot supply another player's score", function()
    GuildData:InitAssistInfoListFromServer({brief(3001)})
    requests[1].callback({result = 0, user_id = 3002, hero_list = {record(47)}})
    assert(GuildData:GetAssistantBriefList()[1].fight_capacity == 0)
end)
check("existing server scores are preserved", function()
    GuildData:InitAssistInfoListFromServer({brief(3001,9999)})
    assert(#requests == 0 and GuildData:GetAssistantBriefList()[1].fight_capacity == 9999)
end)
check("own assist entries retain native filtering", function()
    GuildData:InitAssistInfoListFromServer({brief("5000")})
    assert(#requests == 0 and #GuildData:GetAssistantBriefList() == 0)
end)
check("ordinary team heroes retain native power routing", function()
    getHeroPower = function(id,temp) assert(id == 1039 and not temp); return 4321 end
    local proxy = setmetatable({reserveHeroInfoList = {{type = 1, heroId = 1039}}}, {__index = Proxy})
    assert(proxy:CustomGetHeroPower(1) == 4321)
end)
check("power requests leave the selected foreign hero cache intact", function()
    ForeignInfoData = {selected_owner = 123, selected_hero = {id = 1039}}
    local selected = ForeignInfoData.selected_hero
    GuildData:InitAssistInfoListFromServer({brief(3001)}); reply(1,47)
    assert(ForeignInfoData.selected_owner == 123 and ForeignInfoData.selected_hero == selected)
end)
print("passed " .. passed .. " native guild assist power checks")
