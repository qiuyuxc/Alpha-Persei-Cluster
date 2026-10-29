-- Install on the original ItemUseView used by StoreItemInfo's bag-use button.
return function(View)
    local function coolant(self)
        local info = self.itemData_
        local cfg = info and ItemCfg[info.id]
        if cfg and cfg.sub_type == ItemConst.ITEM_SUB_TYPE.TIME_LIMIT_ITEM then
            cfg = ItemCfg[cfg.param[1]]
        end
        if not cfg or cfg.type ~= ItemConst.ITEM_TYPE.PROPS then
            return nil
        end
        if cfg.sub_type ~= ItemConst.ITEM_SUB_TYPE.PROPS_VITALITY
            and cfg.sub_type ~= ItemConst.ITEM_SUB_TYPE.PROPS_VITALITY_TIME_VALID then
            return nil
        end
        local reward = cfg.param and cfg.param[1]
        if type(reward) == "table" and reward[1] == CurrencyConst.CURRENCY_TYPE_VITALITY
            and type(reward[2]) == "number" and reward[2] > 0 then
            return info, reward[2]
        end
        return nil
    end

    local addVitality = View.AddVitalityCnt
    function View:AddVitalityCnt()
        local info, gain = coolant(self)
        if info then
            return gain
        end
        return addVitality(self)
    end

    local getMaxNum = View.GetMaxNum
    function View:GetMaxNum()
        local info, gain = coolant(self)
        if not info then
            return getMaxNum(self)
        end
        local vitalityID = CurrencyConst.CURRENCY_TYPE_VITALITY
        local owned = ItemTools.getItemNum(info.id, info.timeValid)
        local room = ItemCfg[vitalityID].max - ItemTools.getItemNum(vitalityID)
        return math.max(math.min(owned, math.floor(room / gain)), 1)
    end

    return View
end
