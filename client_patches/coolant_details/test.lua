-- luajit test.lua ORIGINAL_ASSET_DIRECTORY PATCHED_WRAPPER_OR_SOURCE REPORT_JSON
local assets, patched, report = assert(arg[1]), assert(arg[2]), assert(arg[3])
local function original(name)
    return assert(loadfile(assets .. "/" .. name .. ".lua.luac"))()
end
ItemConst, ItemCfg = original("ItemConst"), original("ItemCfg")
CurrencyConst = {CURRENCY_TYPE_VITALITY = 4}
MaterialConst = {MATERIAL_TYPE = {MYTHIC_REFRESH = 408}}
StoreConst = {ITEM_INFO_TYPE = {DESC = 1, SOURCE = 2}}
USE_ITEM_SUCCESS = "useSuccess"
local NOW = 1793275200
gameTimer = {GetNextWeekFreshTime = function() return NOW + 604800 end}
local state, packets, passed = {}, {}, 0
function class(_, parent) return setmetatable({super = parent}, {__index = parent}) end
function clone(source)
    local result = {}
    for k, v in pairs(source) do result[k] = type(v) == "table" and clone(v) or v end
    return result
end
function handler(self, method) return function(...) return method(self, ...) end end
function typeof(value) return value end
function isSuccess(result) return result == 0 end
function ShowTips(code) state.tip = code end
function GetTips(code) return code end
function GetI18NText(text) return text end
function getReward(value) state.reward = value end
function mergeReward(value) return value end
function canAddVitalityByProp() return state.fatigue < ItemCfg[4].max end
OperationRecorder = {Record = function() end}
ItemTemplateData = {}
EventTriggerListener = {}
UnityEngine = {EventSystems = {EventTriggerType = {PointerUp = 1}}}
LuaHelper = {EventTriggerAction1 = function(callback) return callback end}
CurrencyData = {GetCurrencyNum = function() return state.fatigue end}
MaterialData = {GetMaterialNum = function(_, id, valid)
    return state.stock[tostring(id) .. "_" .. tostring(valid or 0)] or 0
end}
manager = {
    time = {GetServerTime = function() return NOW end},
    notify = {RegistListener = function() end, RemoveListener = function() end,
        Invoke = function() state.invokes = state.invokes + 1 end},
    net = {SendWithLoadingNew = function(_, cmd, data, reply, callback)
        assert(cmd == 17012 and reply == 17013)
        state.requests = state.requests + 1
        state.pending = {data = data, callback = callback}
    end}
}
CommonItemView = {New = function() return {SetData = function(self, data) self.data = data end} end}
ReduxView = {
    BindCfgUI = function() end,
    AddBtnListener = function(self, button, target, callback) self.buttons[button or target] = callback end,
    AddPressingByTimeListener = function(self, button, a, b, c, callback) self.presses[button] = callback end,
    Back = function(self) self.backs = self.backs + 1 end
}
ItemTools, CommonAction = original("ItemTools"), original("CommonAction")
local Store, baseline = original("StoreItemInfo"), original("ItemUseView")
local loaded = assert(loadfile(patched))()
local View = type(loaded) == "function" and loaded(original("ItemUseView")) or loaded
local function slider()
    local listeners = {}
    local changed = {AddListener = function(_, callback) listeners[callback] = true end}
    return setmetatable({_value = 1, onValueChanged = changed}, {
        __index = function(self, key) if key == "value" then return self._value end end,
        __newindex = function(self, key, value)
            if key ~= "value" then rawset(self, key, value); return end
            if self._value == value then return end
            self._value = value
            for callback in pairs(listeners) do callback(value) end
        end
    })
end
local function button() return {gameObject = {}} end
local function usage(info, klass)
    local trigger = {AddListenerType1 = function() end}
    local self = setmetatable({params_ = {itemData = info}, buttons = {}, presses = {}, backs = 0,
        useNumSlider_ = slider(), maskBtn_ = button(), cancelBtn_ = button(), okBtn_ = button(),
        addBtn_ = button(), delBtn_ = button(), titleText_ = {}, nameText1_ = {}, useNumText_ = {},
        useNumSliderGo_ = {transform = {GetComponent = function() return trigger end}},
        controllerEx_ = {GetController = function() return {SetSelectedState = function() end} end}
    }, {__index = klass})
    self:Init()
    self:OnEnter()
    return self
end
local selectedClass
JumpTools = {OpenPageByJump = function(page, params)
    assert(page == "itemUse")
    state.page = usage(params.itemData, selectedClass)
    state.pageName = page
end}
local function stock(id, valid, count) state.stock[tostring(id) .. "_" .. tostring(valid or 0)] = count end
local function bag(id, valid, count, klass)
    state = {fatigue = 50, stock = {}, requests = 0, invokes = 0}
    stock(id, valid, count or 3)
    local info = {id = id, number = count or 3, timeValid = valid or 0}
    local self = setmetatable({itemData_ = info, buttons = {},
        switchBtn_ = button(), useBtn_ = button(), composeBtn_ = button(), decomposeBtn_ = button()
    }, {__index = Store})
    self:AddListeners()
    selectedClass = klass or View
    return self
end
local function open(id, valid, count, klass)
    local self = bag(id, valid, count, klass)
    self.buttons[self.useBtn_]()
    assert(state.pageName == "itemUse")
    return state.page
end
local function send(page, id, count, valid, gain)
    page.buttons[page.okBtn_]()
    assert(state.requests == 1)
    local entry = state.pending.data.use_item_list[1]
    assert(entry.item_info.id == id and entry.item_info.num == count)
    assert((entry.item_info.time_valid or 0) == (valid or 0) and #entry.use_list == 0)
    table.insert(packets, {id = id, num = count, time_valid = valid or 0, gain = gain})
end
local function test(name, callback)
    callback(); passed = passed + 1; print("ok - " .. name)
end

test("original permanent coolant works through bag and quantity page", function()
    local page = open(20004, 0, 3, baseline)
    send(page, 20004, 1, 0, 30)
end)
test("original timed coolant fails in the actual confirm callback before sending", function()
    for _, id in ipairs({20013, 20014, 20015}) do
        local page = open(id, NOW + 86400, 3, baseline)
        local ok, errorText = pcall(page.buttons[page.okBtn_])
        assert(not ok and tostring(errorText):find("number value", 1, true))
        assert(state.requests == 0)
    end
end)
test("all timed aliases use actual ID and validity in the original callback", function()
    for id = 20013, 20021 do
        local page = open(id, NOW + 86400, 3)
        local gain = ItemCfg[ItemCfg[id].param[1]].param[1][2]
        send(page, id, 1, NOW + 86400, gain)
    end
end)
test("timed stock with validity zero uses the configured weekly expiry", function()
    local page = open(20013, 0, 3)
    send(page, 20013, 1, 0, 30)
end)
test("timed stock remains separate from its permanent template stock", function()
    local page = open(20013, NOW + 86400, 3)
    stock(20004, 0, 1000)
    assert(page:GetMaxNum() == 3 and page.commonItem1_.data.number == 3)
    assert(page.commonItem1_.data.id == 20013)
    page.presses[page.addBtn_.gameObject]()
    assert(page.selectNum_ == 2 and page.useNumText_.text == "2")
    send(page, 20013, 2, NOW + 86400, 60)
    stock(20013, NOW + 86400, 1); state.fatigue = 110
    state.pending.callback({result = 0, drop_list = {{id = 4, num = 60}}})
    assert(page.commonItem1_.data.number == 1 and page.canUseMaxNum_ == 1)
    assert(MaterialData:GetMaterialNum(20004, 0) == 1000)
end)
test("all permanent sizes keep their configured recovery amounts", function()
    for _, id in ipairs({20004, 20005, 20006}) do
        local page = open(id, 0, 3)
        page.presses[page.addBtn_.gameObject]()
        send(page, id, 2, 0, ItemCfg[id].param[1][2] * 2)
    end
end)
test("timed quantity is capped by both inventory and available fatigue room", function()
    local page = open(20013, NOW + 86400, 20)
    state.fatigue = ItemCfg[4].max - 60
    page:InitData(); page:UpdateView()
    assert(page.canUseMaxNum_ == 2)
    page.presses[page.addBtn_.gameObject]()
    page.presses[page.addBtn_.gameObject]()
    assert(page.selectNum_ == 2)
    send(page, 20013, 2, NOW + 86400, 60)
end)
test("a fatigue change after opening is rechecked on confirm", function()
    local page = open(20013, NOW + 86400, 3)
    state.fatigue = ItemCfg[4].max - 29
    page.buttons[page.okBtn_]()
    assert(state.requests == 0 and state.tip == "ERROR_ITEM_FATIGUE_LIMIT")
end)
test("natural recovery cap does not prevent a coolant refill", function()
    local page = open(20013, NOW + 86400, 3)
    state.fatigue = 240
    send(page, 20013, 1, NOW + 86400, 30)
end)
test("expired timed stock is rejected by the original bag entry", function()
    local self = bag(20013, NOW - 1, 3)
    self.buttons[self.useBtn_]()
    assert(state.page == nil and state.requests == 0 and state.tip == "ITEM_EXPIRE")
end)
test("quantity controls and slider still use the original listeners", function()
    local page = open(20013, NOW + 86400, 3)
    page.presses[page.addBtn_.gameObject](); page.presses[page.addBtn_.gameObject]()
    assert(page.selectNum_ == 3 and not page.addBtn_.interactable)
    page.presses[page.delBtn_.gameObject](); page.presses[page.delBtn_.gameObject]()
    assert(page.selectNum_ == 1 and not page.delBtn_.interactable)
    page.useNumSlider_.value = 0
    assert(page.selectNum_ == 1 and page.useNumSlider_.value == 1)
end)
test("server failure does not change stock or open rewards", function()
    local page = open(20013, NOW + 86400, 3)
    send(page, 20013, 1, NOW + 86400, 30)
    state.pending.callback({result = 3})
    assert(state.tip == 3 and state.reward == nil and state.invokes == 0)
    assert(MaterialData:GetMaterialNum(20013, NOW + 86400) == 3)
end)
test("other props retain their original quantity calculation and callbacks", function()
    local page = open(22001, 0, 3)
    assert(page:GetMaxNum() == baseline.GetMaxNum(page))
    page.presses[page.addBtn_.gameObject]()
    page.buttons[page.okBtn_]()
    assert(state.requests == 1 and state.pending.data.use_item_list[1].item_info.id == 22001)
    assert(state.pending.data.use_item_list[1].item_info.num == 2)
    page.buttons[page.cancelBtn_](); page.buttons[page.maskBtn_]()
    assert(page.backs == 2)
end)
local out = assert(io.open(report, "w"))
out:write('{"passed":', passed, ',"packets":[')
for index, packet in ipairs(packets) do
    if index > 1 then out:write(',') end
    out:write(string.format('{"cmd":17012,"expected_gain":%d,"data":{"use_item_list":[{"item_info":{"id":%d,"num":%d,"time_valid":%d},"use_list":[]}]}}',
        packet.gain, packet.id, packet.num, packet.time_valid))
end
out:write(']}\n'); out:close()
print("CLIENT_ROUTE_TESTS_OK " .. passed)
