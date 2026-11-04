-- Keep native banner ordering and click targets when historical art is absent.
-- Build 307/321 returns sprite paths; Unity resolves them when assigning images.
local availablePaths = {
    ["i18nimg/banner/activity151"] = true,
    ["i18nimg/banner/activity45"] = true,
    ["i18nimg/banner/activity46"] = true,
    ["i18nimg/banner/activity_107701"] = true,
    ["i18nimg/banner/activity_1077"] = true,
    ["i18nimg/banner/activity_116601"] = true,
    ["i18nimg/banner/activity_1166"] = true,
    ["i18nimg/banner/activity_rush"] = true,
    ["i18nimg/banner/activity_shouchong"] = true
}

local function hasImage(sprite)
    local kind = type(sprite)
    if kind == "string" then
        return availablePaths[string.lower(sprite)] == true
    end
    if kind ~= "userdata" and kind ~= "table" then
        return false
    end
    if type(isNil) ~= "function" then
        return true
    end
    local ok, empty = pcall(isNil, sprite)
    return ok and not empty
end

return function(Tools)
    local original = Tools.GetCurrentAdvInfo

    function Tools.GetCurrentAdvInfo()
        local visible = {}
        local fallback
        local triedFallback = false

        for _, entry in ipairs(original() or {}) do
            if not hasImage(entry.image) then
                local config = MainAdvinfoCfg[entry.id]
                -- System link 404 is native draw navigation. Keep its pool ID.
                if config and config.type == 1 and config.parameter
                    and config.parameter[1] == 404 then
                    if not triedFallback then
                        triedFallback = true
                        local ok, sprite = pcall(getSpriteWithoutAtlas, "I18NImg/Banner/activity46")
                        if ok and hasImage(sprite) then
                            fallback = sprite
                        end
                    end
                    entry.image = fallback
                end
            end
            if hasImage(entry.image) then
                visible[#visible + 1] = entry
            end
        end

        return visible
    end

    return Tools
end
