-- Derive missing guild assist power with the native calculator and owner data.
local state = rawget(_G, "__AGGuildAssistPower")
if not state then
    state = {epoch = 0, entries = {}}
    rawset(_G, "__AGGuildAssistPower", state)
end

local function ownerKey(value)
    return tostring(value or "0")
end

local function currentPower(info)
    for _, entry in ipairs(state.entries) do
        if ownerKey(entry.member_id) == ownerKey(info.member_id)
            and entry.assist_hero_id == info.assist_hero_id then
            return entry.fight_capacity or 0
        end
    end
    return info.fight_capacity or 0
end

local function loadMissingPower(data, epoch, entries, guildID)
    local owners = {}
    for _, entry in ipairs(entries) do
        if not entry.fight_capacity or entry.fight_capacity == 0 then
            owners[ownerKey(entry.member_id)] = entry.member_id
        end
    end
    for key, owner in pairs(owners) do
        local expectedKey, expectedOwner = key, owner
        manager.net:SendWithLoadingNew(32020, {user_id = expectedOwner, type = 2}, 32021,
            function(reply)
                local guild = data:GetGuildInfo()
                if state.epoch ~= epoch or not data.bossData_
                    or data.bossData_.assist_info_list ~= entries
                    or not guild or guild.id ~= guildID or not reply
                    or reply.result ~= 0 or ownerKey(reply.user_id) ~= expectedKey then
                    return
                end
                local changed = false
                for _, record in ipairs(reply.hero_list or {}) do
                    local base = record.hero_base_info
                    if base then
                        for _, entry in ipairs(entries) do
                            if ownerKey(entry.member_id) == expectedKey
                                and entry.assist_hero_id == base.id
                                and (not entry.fight_capacity or entry.fight_capacity == 0) then
                                -- Local templates leave ForeignInfoData and the selected
                                -- borrowed hero's global cache in their current state.
                                local ok, power = pcall(function()
                                    local hero = ForeignHeroDataTemplate.New(base.id)
                                    hero:Init(record)
                                    return calcBattlePower(hero, hero:GetServantInfo(), hero.equip_list)
                                end)
                                if ok and type(power) == "number" and power == power
                                    and power >= 0 and power < 4294967296 then
                                    entry.fight_capacity = math.floor(power)
                                    changed = true
                                elseif not ok then
                                    print("Guild assist power calculation failed: " .. tostring(power))
                                end
                            end
                        end
                    end
                end
                if changed then
                    manager.notify:Invoke(GUILD_ASSIST_BRIEF_UPDATE, {})
                end
            end)
    end
end

return function(asset, Tools)
    if asset == "GuildData.lua" then
        local original = Tools.InitAssistInfoListFromServer
        function Tools.InitAssistInfoListFromServer(self, list)
            state.epoch = state.epoch + 1
            local epoch = state.epoch
            original(self, list)
            local entries = self.bossData_.assist_info_list
            state.entries = entries
            loadMissingPower(self, epoch, entries, self:GetGuildInfo().id)
        end
    elseif asset == "GuildBossAssistantHeadItemView.lua" then
        local original = Tools.AddEventListeners
        function Tools.AddEventListeners(self)
            original(self)
            self:RegistEventListener(GUILD_ASSIST_BRIEF_UPDATE, function()
                if self.data_ then
                    self:UpdateView()
                end
            end)
        end
    elseif asset == "GuildBossSelectHeroProxy.lua" then
        local original = Tools.CustomGetHeroPower
        function Tools.CustomGetHeroPower(self, index, ...)
            local member = self.reserveHeroInfoList[index]
            if member and member.type == 2 then
                return currentPower(member.info)
            end
            return original(self, index, ...)
        end
    else
        error("Unsupported guild assist power asset: " .. tostring(asset))
    end
    return Tools
end
