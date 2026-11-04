local root = assert(arg[1])
local wrapper = arg[2]
local count = 0
local function check(value, reason) count=count+1;assert(value,reason) end
function class(_, base)
    local value={super=base};value.__index=value
    setmetatable(value,{__index=base})
    function value.New(...) local self=setmetatable({},value);self:OnCtor(...);return self end
    return value
end
ReduxView={}
function ReduxView:AdaptScreen() end
function ReduxView:AddBtnListener(button, _, callback) button.callback=callback end
function ReduxView:Dispose() self.disposed=true end
local function controller()
    return {SetSelectedState=function(self,state) self.state=state end}
end
local loadedRoles, files, stopped = {}, {}, 0
local luaArrays = false
local function transform()
    return {Find=function()return nil end,SetAsFirstSibling=function(self)self.first=true end}
end
function ReduxView:BindCfgUI()
    self.controllerEx_={GetController=function(_,name) return controller() end}
    self.detailBtn_={};self.changeUpBtn_={gameObject={activeSelf=true}}
    self.changeCountText_={gameObject={activeSelf=true}}
    self.roleContentTrans_=transform();self.heroName_={};self.heroRareIcon_={};self.heroRaceIcon_={}
    self.maskGo_={activeSelf=false};self.countdownText_={gameObject={activeSelf=true}}
end
function SetActive(object, value) if object then object.activeSelf=value end end
function handler(object, method) return function(state) return method(object,state) end end
function SetFile(player, _, path) player.path=path;files[#files+1]=path end
function typeof(value)return value end
UnityEngine={UI={Image="Image",RawImage="RawImage"}}
TimeTools={StartAfterSeconds=function(_,callback)callback()end}
HeroTools=setmetatable({GetHeroFullName=function(id)return "hero-"..id end},
    {__index=function(_,key)return function(id)return key..id end end})
DrawData={GetPollUpID=function() error("Fixed display must not change or read selectable UP state") end,
    GetIsFirstSSR=function()return true end,GetPoolUpTimes=function()return 0 end}
manager={video={GetVedioTrackIndex=function()return 0 end},time={GetLostTimeStrWith3Unit=function(_,n)return tostring(n) end}}
function GetTips()return "%s" end
JumpTools={OpenPageByJump=function(_,data)check(data.hid==1199,"Preview keeps fixed UP hero")end}
DrawTools={GoToSelectUpHeroView=function()error("Fixed display must not enable changing UP")end}
Object={Destroy=function(object)if object then object.destroyed=true end end}
GameObject={}
local function movie()
    local player={status="Ready",SetAudioTrack=function()end,SetSubtitleChannel=function()end}
    local video={player=player,Play=function()player.status="Playing" end,
        Stop=function()stopped=stopped+1;player.status="Stop" end}
    local helper={enabled=true}
    local go={activeSelf=true,transform=transform()}
    local image={enabled=true}
    function go:GetComponentsInChildren()return luaArrays and {image} or {[0]=image,Length=1}end
    function go:GetComponent(name) return name=="CriManaExMultipleVedio" and helper or video end
    return go
end
function Object.Instantiate(prefab, parent)
    if prefab.kind=="movie" then return movie() end
    local go={activeSelf=true,transform=transform(),kind=prefab.kind}
    local image={enabled=true}
    local background={GetComponentsInChildren=function()return {[0]=image,Length=1}end}
    go.transform.Find=function(_,name)return name=="background" and background or nil end
    if prefab.kind=="role" then loadedRoles[#loadedRoles+1]=prefab.path end
    return go
end
GameObject.Instantiate=Object.Instantiate
Asset={Load=function(path)
    if path=="Widget/System/Pool/pool_4000301" then
        return {transform={Find=function()return {gameObject={kind="movie"}}end}}
    end
    return {kind=path:find("PoolRole/",1,true) and "role" or "pool",path=path}
end}
DrawPoolCfg=assert(loadfile(root.."/DrawPoolCfg.lua.luac"))()
DrawRolePool=assert(loadfile(root.."/DrawRolePool.lua.luac"))()
local catalog=assert(loadfile("catalog.lua"))()
local Base
if wrapper then Base=assert(loadfile(wrapper))()
else
    Base=assert(loadfile(root.."/DrawBasePool.lua.luac"))()
    Base=assert(loadfile("patch.lua"))()(Base,catalog)
end
local p=Base.New(transform(),2040302,2040302)
p:Refresh()
check(p:GetPoolId()==2040302 and p:GetShowId()==2040302,"Pool IDs unchanged")
check(p._displayRole.upId_==1199,"Correct fixed UP")
check(p._displayRole.heroName_.text=="hero-1199","Same hero rendered")
check(loadedRoles[1]=="Widget/System/PoolRole/PoolRole_1199","Current same-hero prefab")
check(not p._displayRole.changeUpBtn_.gameObject.activeSelf,"Fixed UP cannot be changed from display")
check(p._displayRole.firstController_.state=="false","No incorrect permanent-pool guarantee badge")
p._displayRole.detailBtn_.callback()
p:SetActive(true)
check(files[#files]=="SofdecAsset/function/draw/1199_start.usm","Same-hero entry animation")
check(p._displayMovie.activeSelf and p._displayRole.gameObject_.activeSelf,"Entry movie and background active")
check(not p._displayImages[1].enabled,"Empty video renderer stays hidden until first decoded frame")
local callback=p._displayPlayer.statusChangeCallback
callback("Playing")
check(p._displayImages[1].enabled,"Decoded entry frame becomes visible")
callback("PlayEnd")
check(not p._displayMovie.activeSelf and p._displayRole.gameObject_.activeSelf,"Entry ends on current role background")
local number=#files;p:SetActive(true)
check(#files==number,"Repeated refresh does not replay entry")
p:SetActive(false)
check(p._displayPlayer.statusChangeCallback==nil,"Hidden movie drops callback")
callback("Playing")
check(not p._displayMovie.activeSelf,"Late callback cannot reopen hidden movie")
p:SetActive(true)
check(#files==number+1,"Re-entering pool replays entry once")
p._displayPlayer.statusChangeCallback("Error")
check(not p._displayMovie.activeSelf and p._displayRole.gameObject_.activeSelf,"Video error preserves role background")
p:SetCountDownText(123)
check(p._displayRole.countdownText_.text=="123","Countdown delegated")
p:Dispose();check(p.disposed,"Display cleanup")
local v=Base.New(transform(),4010302,4010302)
v:SetActive(true)
check(v._displayRole==nil and v._displayMovie.transform.first,"Original layout keeps repaired video behind controls")
check(v._displayPlayer.path=="SofdecAsset/function/draw/1020_start.usm","Current same-hero intro for absent old PV")
v._displayPlayer.statusChangeCallback("Playing")
check(not v._oldBackgroundImages[1].enabled,"Old blank background does not cover current video")
v._displayPlayer.statusChangeCallback("PlayEnd")
check(v._displayPlayer.path=="SofdecAsset/function/draw/1020_loop.usm" and v._displayPlayer.loop,"Same-hero loop supplies background after entry")
v._displayPlayer.statusChangeCallback("StopProcessing")
check(v.gameObject_.activeSelf and v._displayMovie.activeSelf,"Intermediate loop state keeps pool visible")
v._displayPlayer.statusChangeCallback("Error")
check(v._oldBackgroundImages[1].enabled and not v._displayMovie.activeSelf,"Failed video restores original background layer")
v:SetActive(false);v:Dispose()
local delayed
TimeTools.StartAfterSeconds=function(_,callback)delayed=callback end
local closing=Base.New(transform(),2040302,2040302)
number=#files;closing:SetActive(true);closing:SetActive(false);delayed()
check(#files==number,"Queued start cannot play after closing")
closing:Dispose()
luaArrays=true
local shaped=Base.New(transform(),2100303,2100303)
check(#shaped._displayImages==1,"Lua array form of component results supported")
shaped:Dispose()
check(catalog[4000301]==nil and catalog[4000601]==nil and catalog[5020303]==nil,"Normal Phantom pool stays native")
check(catalog[5030601]==nil and catalog[5030301]==nil and catalog[10002]==nil,"Selectable and servant pools untouched")
local roleConfig=DrawPoolCfg[2040302]
check(roleConfig.unoption_up_items[1][1]==1199,"Native reward config untouched")
local native={poolId=4000601,gameObject_={activeSelf=true},maskGo_={activeSelf=false},movie=movie()}
native.transform_={Find=function()return native.movie end}
native.movie:GetComponent("CriManaMovieControllerForUI").player.status="Error"
Base.UpdateView(native)
check(not native.maskGo_.activeSelf and not native.movie.activeSelf,"Initial video error cannot cover background")
Base.CirMovieStatusChange(native,"StopProcessing");Base.CirMovieStatusChange(native,"Stop")
check(native.gameObject_.activeSelf,"StopProcessing does not hide pool background")
Base.CirMovieStatusChange(native,"Error")
check(not native.maskGo_.activeSelf,"Error callback clears blocking mask")
check(stopped>=2,"Players stopped on hide and dispose")
print("GM-independent gacha background compatibility: "..count.." cases passed")
