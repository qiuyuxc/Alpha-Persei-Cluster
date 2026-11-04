-- The catalog contains fixed UP pools whose historical display assets are gone.
return function(Base, catalog)
    local original = {}
    for _, name in ipairs({"OnCtor", "Refresh", "SetActive", "UpdateView", "Dispose",
        "ShowCountDownText", "SetCountDownText", "ShowRightPanel", "CirMovieStatusChange"}) do
        original[name] = Base[name]
    end

    local function active(object, value)
        if object ~= nil then SetActive(object, value) end
    end
    local function imagesUnder(root, kind)
        local array = root:GetComponentsInChildren(typeof(kind), true)
        local images = {}
        if array == nil then return images end
        local length = tonumber(array.Length)
        if length then
            for index = 0, length - 1 do images[#images + 1] = array[index] end
        elseif type(array) == "table" then
            for _, image in ipairs(array) do images[#images + 1] = image end
        end
        return images
    end
    local function clearMask(self)
        active(self.maskGo_, false)
    end
    local function stopClip(self)
        self._displayTicket = (self._displayTicket or 0) + 1
        if not self._displayMovie then return end
        self._displayPlayer.statusChangeCallback = nil
        self._displayController:Stop()
        active(self._displayMovie, false)
        clearMask(self)
    end
    local function backgroundOnly(self)
        for _, image in ipairs(self._oldBackgroundImages or {}) do image.enabled = false end
    end
    local function playFile(self, path, looping)
        self._displayPhase = looping and "loop" or "start"
        -- SetFile resolves the existing manifest path through the native loader.
        SetFile(self._displayPlayer, nil, path)
        self._displayPlayer.loop = looping
        active(self._displayMovie, true)
        self._displayController:Play()
    end
    local function clipStatus(self, state)
        if not self._displayVisible then return end
        local status = tostring(state)
        if status == "Playing" then
            clearMask(self)
            for _, image in ipairs(self._displayImages or {}) do image.enabled = true end
            if not self._displayRole then backgroundOnly(self) end
        elseif status == "PlayEnd" then
            clearMask(self)
            if self._displayRole then
                active(self._displayMovie, false)
            elseif self._displayConfig.loop then
                playFile(self, self._displayConfig.loop, true)
            end
        elseif status == "Error" then
            clearMask(self)
            active(self._displayMovie, false)
            for _, image in ipairs(self._oldBackgroundImages or {}) do image.enabled = true end
        end
        -- StopProcessing is an intermediate player state, not a reason to hide
        -- the pool. The next valid frame replaces the previous displayed frame.
    end
    local function createClip(self, parent)
        if not self._displayRole then
            local old = parent:Find("movie")
            if old then
                local oldHelper = old:GetComponent("CriManaExMultipleVedio")
                if oldHelper then oldHelper.enabled = false end
                local oldController = old:GetComponent("CriManaMovieControllerForUI")
                if oldController then oldController:Stop(); oldController.enabled = false end
                for _, image in ipairs(imagesUnder(old, UnityEngine.UI.RawImage)) do image.enabled = false end
            end
        end
        local template = Asset.Load("Widget/System/Pool/pool_4000301")
        if not template then return end
        local movie = template.transform:Find("movie")
        if not movie then return end
        local go = Object.Instantiate(movie.gameObject, parent)
        go.name = "compatibility_movie"
        active(go, false)
        local helper = go:GetComponent("CriManaExMultipleVedio")
        if helper then helper.enabled = false end
        local controller = go:GetComponent("CriManaMovieControllerForUI")
        if not controller or not controller.player then Object.Destroy(go); return end
        -- Do not let the cloned controller's first Start restore the template's
        -- original hero path over the selected pool's current video.
        pcall(function() controller.moviePath = "" end)
        self._displayMovie, self._displayController, self._displayPlayer = go, controller, controller.player
        self._displayImages = imagesUnder(go, UnityEngine.UI.RawImage)
        for _, image in ipairs(self._displayImages) do image.enabled = false end
        if not self._displayRole then
            go.transform:SetAsFirstSibling()
            self._oldBackgroundImages = {}
            for _, name in ipairs({"background", "roleBg", "mask", "mask_left", "mask_bottom", "jianbianBg"}) do
                local root = parent:Find(name)
                if root then
                    for _, image in ipairs(imagesUnder(root, UnityEngine.UI.Image)) do
                        self._oldBackgroundImages[#self._oldBackgroundImages + 1] = image
                    end
                end
            end
        end
    end
    local function startClip(self)
        if not self._displayMovie then return end
        self._displayTicket = (self._displayTicket or 0) + 1
        local ticket = self._displayTicket
        for _, image in ipairs(self._displayImages or {}) do image.enabled = false end
        active(self._displayMovie, true)
        local function ready()
            if not self._displayVisible or self._displayTicket ~= ticket then return end
            self._displayPlayer.statusChangeCallback = function(state) clipStatus(self, state) end
            if self.SetVideoTrack then
                self:SetVideoTrack(self._displayPlayer, self._displayConfig.start)
            end
            playFile(self, self._displayConfig.start, false)
        end
        -- Native DrawView uses the same one-frame delay for newly created UI.
        TimeTools.StartAfterSeconds(0.02, ready)
    end

    function Base.OnCtor(self, parent, poolId, showId)
        self._displayConfig = catalog[poolId]
        if self._displayConfig and self._displayConfig.role then
            local prefab = Asset.Load("Widget/System/Pool/pool_convention_mult")
            if prefab then
                self.poolId, self.showId = poolId, showId
                self._displayRole = DrawRolePool.New(Object.Instantiate(prefab, parent), poolId, showId)
                local hero = self._displayConfig.hero
                self._displayRole.Refresh = function(view)
                    -- Fixed UP presentation stays separate from selectable-pool
                    -- state and never changes DrawData or the server's UP.
                    view.upId_ = hero
                    view:UpdateChooseStatus()
                    view.firstController_:SetSelectedState("false")
                    view:UpdateChangeCount()
                    view:UpdateRole()
                    if view.changeUpBtn_ then active(view.changeUpBtn_.gameObject, false) end
                end
                createClip(self, self._displayRole.transform_)
                return
            end
        end
        original.OnCtor(self, parent, poolId, showId)
        if self._displayConfig then createClip(self, self.transform_) end
    end
    function Base.Refresh(self, ...)
        if self._displayRole then return self._displayRole:Refresh(...) end
        return original.Refresh(self, ...)
    end
    function Base.SetActive(self, value)
        local entering = value and not self._displayVisible
        self._displayVisible = value
        if not value then stopClip(self) end
        if self._displayRole then self._displayRole:SetActive(value)
        else original.SetActive(self, value) end
        if entering and self._displayConfig then startClip(self) end
    end
    function Base.UpdateView(self, ...)
        if self._displayRole then return end
        local result = original.UpdateView(self, ...)
        local state = self.criPlay and tostring(self.criPlay.status)
        if state == "Error" or state == "StopProcessing" or state == "Stop" then
            clearMask(self)
            active(self.movie, false)
        end
        return result
    end
    function Base.CirMovieStatusChange(self, state)
        local status = tostring(state)
        if status == "Error" or status == "StopProcessing" then
            clearMask(self)
            active(self.movie, false)
            return
        end
        return original.CirMovieStatusChange(self, state)
    end
    for _, name in ipairs({"ShowCountDownText", "SetCountDownText", "ShowRightPanel"}) do
        local method = name
        Base[method] = function(self, ...)
            if self._displayRole then return self._displayRole[method](self._displayRole, ...) end
            return original[method](self, ...)
        end
    end
    function Base.Dispose(self)
        self._displayVisible = false
        stopClip(self)
        if self._displayRole then
            self._displayRole:Dispose()
            self._displayRole = nil
            return Base.super.Dispose(self)
        end
        return original.Dispose(self)
    end
    return Base
end
