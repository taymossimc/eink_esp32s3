#include "config_portal.h"

#include "EPD_13in3e.h"
#include "cache/frame_cache.h"
#include "carousel/carousel.h"
#include "config/device_config.h"
#include "display/display_panel.h"
#include "display/display_identity.h"
#include "image/image_pipeline.h"
#include "log/phone_home_log.h"
#include "modem/modem.h"
#include "network/network_manager.h"
#include "power/power.h"
#include "sms/sms_store.h"
#include "storage/usb_campaign_storage.h"
#include "tmads/tmads_campaign_cache.h"

#include <ArduinoJson.h>
#include <DNSServer.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <string.h>
#include <time.h>

#include "battery/ups_battery.h"
#include "tmads/tmads_identity.h"

#include <freertos/task.h>

namespace {

constexpr size_t kMaxUploadBytes = 3U * 1024U * 1024U;
constexpr uint16_t kDnsPort = 53;
WebServer gServer(80);
DNSServer gDns;
TaskHandle_t gTask = nullptr;
SemaphoreHandle_t gActionMutex = nullptr;
String gSsid;
String gPortalKey;
String gCsrfToken;
bool gRunning = false;

// First-run setup happens on the captive AP without credentials; requests
// arriving over the station (LAN) interface must present the device key.
bool requestFromSoftAp() {
    const IPAddress remote = gServer.client().remoteIP();
    const IPAddress ap = WiFi.softAPIP();
    return remote[0] == ap[0] && remote[1] == ap[1] && remote[2] == ap[2];
}

void ensurePortalKey() {
    Preferences prefs;
    if (!prefs.begin("portal", false)) return;
    gPortalKey = prefs.getString("key", "");
    if (gPortalKey.isEmpty()) {
        char generated[13] = {};
        snprintf(generated, sizeof(generated), "%06lX%06lX",
                 static_cast<unsigned long>(esp_random() & 0xFFFFFF),
                 static_cast<unsigned long>(esp_random() & 0xFFFFFF));
        gPortalKey = generated;
        prefs.putString("key", gPortalKey);
    }
    prefs.end();
}

bool portalAuthorized() {
    if (requestFromSoftAp()) return true;
    if (gPortalKey.isEmpty()) return false;
    const String header = gServer.header("X-Portal-Key");
    if (header.length() && header == gPortalKey) return true;
    return gServer.hasArg("key") && gServer.arg("key") == gPortalKey;
}

struct PendingActions {
    uint8_t *upload = nullptr;
    size_t uploadLength = 0;
    char uploadName[96] = {};
    bool uploadReady = false;
    bool uploadCommitted = false;
    bool manualRefresh = false;
    bool bandwidthReady = false;
    bool bandwidthCellular = false;
    size_t bandwidthBytes = 1024U * 1024U;
};

PendingActions gActions;
String gLastAction = "idle";
String gBandwidthResult = "not run";

void addFrameJson(JsonObject object, FrameCacheSlot slot);

class ActionLock {
  public:
    ActionLock() {
        locked_ = gActionMutex &&
                  xSemaphoreTake(gActionMutex, pdMS_TO_TICKS(2000)) == pdTRUE;
    }
    ~ActionLock() {
        if (locked_) xSemaphoreGive(gActionMutex);
    }
    bool locked() const { return locked_; }
  private:
    bool locked_ = false;
};

const char kPortalHtml[] PROGMEM = R"HTML(
<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>E-Ink Device Setup</title><style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}body{max-width:980px;margin:auto;padding:16px}
nav{display:flex;gap:6px;flex-wrap:wrap;position:sticky;top:0;background:Canvas;padding:8px 0}
button,.btn,input,select{font:inherit;padding:9px;border:1px solid #888;border-radius:6px}input[type=checkbox]{width:auto}
button,.btn{cursor:pointer;background:#2463eb;color:white;text-decoration:none}section{display:none}section.on{display:block}
.card{border:1px solid #8888;border-radius:9px;padding:14px;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
label{display:grid;gap:4px}input{width:100%;box-sizing:border-box}.muted{opacity:.7}.msg{white-space:pre-wrap;border-bottom:1px solid #8884;padding:8px 0}
img{max-width:100%;max-height:360px;border:1px solid #888}.ok{color:#198754}.bad{color:#dc3545}
</style></head><body><h1>E-Ink Device Setup</h1><div id=summary class=card>Loading…</div>
<nav><button onclick=tab('cell')>Cellular</button><button onclick=tab('wifi')>Wi-Fi</button><button onclick=tab('sms')>SMS</button><button onclick=tab('home')>Phone Home</button><button onclick=tab('media')>Media</button><button onclick=tab('speed')>Bandwidth</button><button onclick=tab('diag')>Diagnostics</button></nav>
<section id=cell><h2>Cellular / SIM</h2><form id=cellForm class=card><div class=grid>
<label><span>Automatic carrier APN</span><select name=autoApn><option value=1>Enabled</option><option value=0>Manual</option></select></label>
<label><span>APN</span><input name=apn placeholder="sp.koodo.com"></label>
<label><span>Username</span><input name=user autocomplete=off></label><label><span>New password</span><input name=pass type=password autocomplete=new-password></label>
<label><span>PDP type</span><select name=pdp><option>IPv4v6</option><option>IPv4</option></select></label>
<label><span>Authentication</span><select name=auth><option>Auto</option><option>None</option><option>PAP</option><option>CHAP</option></select></label>
<label><span>SIM PIN</span><input name=pin type=password maxlength=8 placeholder="unchanged"></label><label><span>Clear secrets</span><span><input name=clearPass type=checkbox value=1> APN password <input name=clearPin type=checkbox value=1> SIM PIN</span></label>
<label><span>Allow roaming</span><select name=roaming><option value=1>Yes</option><option value=0>No</option></select></label>
</div><p class=muted>Common suggestions: Koodo sp.koodo.com, AT&amp;T broadband, T-Mobile fast.t-mobile.com, Verizon vzwinternet. MVNOs may require their own APN.</p><button>Save cellular settings</button></form></section>
<section id=wifi><h2>Wi-Fi</h2><div class=card><label><span>Global Wi-Fi</span><select id=wifiEnabled><option value=1>Enabled</option><option value=0>Disabled</option></select></label><p><button onclick="saveWifiToggle()">Save toggle</button> <button onclick="scanWifi()">Scan networks</button></p><p class=muted>Tap a network to join it immediately.</p><div id=scan></div></div>
<form id=wifiForm class=card><div class=grid><label>Profile slot<select name=slot><option>0</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option><option>7</option></select></label><label>SSID<input name=ssid required maxlength=32></label><label>Password<input name=password type=password maxlength=64 placeholder="unchanged"></label><label>Enabled<select name=enabled><option value=1>Yes</option><option value=0>No</option></select></label><label><span><input name=clearPassword type=checkbox value=1> Clear saved password</span></label></div><button>Save network</button></form><div id=profiles></div></section>
<section id=sms><h2>SMS Inbox</h2><p><button onclick=loadSms()>Refresh</button> <button onclick=clearSms()>Clear archive</button></p><div id=smsList></div></section>
<section id=home><h2>Phone Home Events</h2><p><button onclick=loadLogs()>Refresh</button> <button onclick=clearLogs()>Clear log</button></p><div id=logs></div></section>
<section id=media><h2>Media</h2><div class=grid><div class=card><h3>Current</h3><div id=currentMeta></div><img src="/media/current.bmp" onerror="this.style.display='none'"><p><a class=btn href="/media/current.bmp?download=1">Download</a></p></div><div class=card><h3>Next</h3><div id=nextMeta></div><img src="/media/next.bmp" onerror="this.style.display='none'"><p><a class=btn href="/media/next.bmp?download=1">Download</a></p></div></div>
<form id=orientationForm class=card><label>Display orientation<select name=orientation><option value=0>Portrait</option><option value=90>Landscape clockwise</option><option value=180>Portrait inverted</option><option value=270>Landscape counterclockwise</option></select></label><button>Save orientation</button></form>
<form id=uploadForm class=card enctype=multipart/form-data><label>Upload JPEG, PNG, WebP, or SVG as next<input name=media type=file accept=".jpg,.jpeg,.png,.webp,.svg,image/jpeg,image/png,image/webp,image/svg+xml" required></label><p class=muted id=convertNote>WebP and SVG are converted in this browser before upload.</p><button>Upload as Next</button></form>
<div class=card><h3>Campaign cache</h3><div id=campaignSummary>Loading…</div></div>
<p><button onclick=refreshDisplay()>Refresh now and queue old current</button></p></section>
<section id=speed><h2>Bandwidth Test</h2><div class=card><label>Download size<select id=speedBytes><option value=262144>256 KB</option><option value=1048576 selected>1 MB</option><option value=5242880>5 MB</option></select></label><p><button onclick="speed('wifi')">Test Wi-Fi</button> <button onclick="speed('cellular')">Test cellular</button></p><div id=speedResult></div></div></section>
<section id=diag><h2>Diagnostics</h2><button onclick=loadStatus()>Refresh</button><pre id=diagnostics class=card></pre></section>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function tab(id){document.querySelectorAll('section').forEach(x=>x.classList.toggle('on',x.id==id));if(id=='sms')loadSms();if(id=='home')loadLogs();if(id=='media')loadMedia()}tab('cell');
let deviceKey=localStorage.getItem('portalKey')||'';
let csrfToken='';
async function api(url,opt){opt=opt||{};const mutation=(opt.method||'GET').toUpperCase()!='GET';opt.headers=Object.assign({},opt.headers,deviceKey?{'X-Portal-Key':deviceKey}:{},mutation&&csrfToken?{'X-CSRF-Token':csrfToken}:{});let r=await fetch(url,opt);
if(r.status==401){const entered=prompt('Enter the device key (shown on the setup AP diagnostics page):');if(entered){deviceKey=entered.trim();localStorage.setItem('portalKey',deviceKey);return api(url,opt)}}
let t=await r.text();if(!r.ok)throw Error(t);try{return JSON.parse(t)}catch{return t}}
function formPost(form,url){form.addEventListener('submit',async e=>{e.preventDefault();try{await api(url,{method:'POST',body:new URLSearchParams(new FormData(form))});await loadStatus();alert('Saved')}catch(x){alert(x)}})}
formPost($('#cellForm'),'/api/config/cellular');formPost($('#wifiForm'),'/api/config/wifi');formPost($('#orientationForm'),'/api/config/orientation');
let currentOrientation=0;
async function convertForUpload(file){
const name=(file.name||'').toLowerCase();
if(name.endsWith('.jpg')||name.endsWith('.jpeg')||name.endsWith('.png'))return file;
const landscape=currentOrientation==90||currentOrientation==270;
const w=landscape?1600:1200,h=landscape?1200:1600;
let bmp;
if('createImageBitmap' in window&&!name.endsWith('.svg')){bmp=await createImageBitmap(file,{imageOrientation:'from-image'})}
else{const url=URL.createObjectURL(file);bmp=await new Promise((ok,err)=>{const i=new Image();i.onload=()=>ok(i);i.onerror=err;i.src=url});bmp._url=url}
const sw=bmp.width||bmp.naturalWidth,sh=bmp.height||bmp.naturalHeight;
if(!sw||!sh)throw Error('Image could not be decoded in this browser');
const c=document.createElement('canvas');c.width=w;c.height=h;const ctx=c.getContext('2d');
ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);
const s=Math.max(w/sw,h/sh);const dw=sw*s,dh=sh*s;
ctx.drawImage(bmp,(w-dw)/2,(h-dh)/2,dw,dh);
if(bmp.close)bmp.close();if(bmp._url)URL.revokeObjectURL(bmp._url);
const blob=await new Promise(ok=>c.toBlob(ok,'image/jpeg',0.92));
if(!blob)throw Error('Browser conversion failed');
return new File([blob],name.replace(/\.[^.]+$/,'')+'.jpg',{type:'image/jpeg'});
}
$('#uploadForm').onsubmit=async e=>{e.preventDefault();try{
const raw=e.target.media.files[0];if(!raw)return;
$('#convertNote').textContent='Preparing image…';
let converted;
try{converted=await convertForUpload(raw)}
catch(browserError){
  const lower=(raw.name||'').toLowerCase();
  if(!lower.endsWith('.webp')&&!lower.endsWith('.svg'))throw browserError;
  converted=raw;
  $('#convertNote').textContent='Browser conversion unavailable; using device decoder…';
}
const body=new FormData();body.set('media',converted,converted.name);
await api('/api/media/upload',{method:'POST',body});
$('#convertNote').textContent='Uploaded '+converted.name+' ('+Math.round(converted.size/1024)+' KB)';
alert('Upload queued for processing');setTimeout(loadMedia,2500)}catch(x){$('#convertNote').textContent='';alert(x)}};
async function loadStatus(){let d=await api('/api/status');csrfToken=d.csrfToken||csrfToken;currentOrientation=d.config.orientation;
let batt=d.battery&&d.battery.available?` · Battery ${d.battery.socPercent!=null?d.battery.socPercent+'%':(d.battery.voltageV.toFixed(2)+' V')}`:'';
$('#summary').innerHTML=`AP <b>${esc(d.ap.ssid)}</b> · ${esc(d.ap.ip)} · 5V ${d.rail?'ON':'OFF'}${batt} · ${esc(d.action)}`;
$('#diagnostics').textContent=JSON.stringify(d,null,2);$('#wifiEnabled').value=d.config.wifiEnabled?1:0;let f=$('#cellForm').elements;f.autoApn.value=d.config.autoApn?1:0;f.apn.value=d.config.apn;f.user.value=d.config.user;f.pdp.value=d.config.pdp;f.auth.value=d.config.auth;f.roaming.value=d.config.roaming?1:0;$('#orientationForm').elements.orientation.value=d.config.orientation;$('#profiles').innerHTML=d.config.profiles.map((p,i)=>`<div class=card>Slot ${i}: <b>${esc(p.ssid||'(empty)')}</b> ${p.enabled?'enabled':'disabled'} ${p.hasPassword?'(password saved)':''}</div>`).join('');$('#speedResult').textContent=d.bandwidth;
if(d.campaignCache){let c=d.campaignCache;let mib=b=>(b/1048576).toFixed(1)+' MiB';
$('#campaignSummary').innerHTML=`Catalog: <b>${c.cachedCount??0}/${c.assetCount??0}</b> assets cached · USB campaign data ${mib(c.usbCampaignBytes||0)} (${c.usbActive?'USB active':'USB absent'}) · LittleFS framebuffers ${mib(c.littlefsUsedBytes||0)} of ${mib(c.littlefsTotalBytes||0)}<br>`+
(c.assets||[]).map(a=>`<small>${esc(a.campaignId)} [${esc(a.status)}] ${a.eligible?'eligible':'preload'} ${a.cached?'✓cached':'…missing'}</small>`).join('<br>')}}
async function joinWifi(ssid,secured){let password='';if(secured){password=prompt(`Password for ${ssid}:`)||'';if(secured&&!password)return}
$('#scan').innerHTML='Joining '+esc(ssid)+'…';
try{let d=await api('/api/wifi/join',{method:'POST',body:new URLSearchParams({ssid,password})});
$('#scan').innerHTML=d.connected?`Connected to ${esc(d.ssid)} at ${esc(d.ip)}`:'Saved, but connection failed — check the password';loadStatus()}catch(x){$('#scan').innerHTML='';alert(x)}}
async function scanWifi(){$('#scan').textContent='Scanning…';let d=await api('/api/wifi/scan');$('#scan').innerHTML=d.networks.map(n=>`<button type=button onclick='joinWifi(${JSON.stringify(n.ssid)},${n.secured})'>${esc(n.ssid)} (${n.rssi} dBm${n.secured?', secured':''})</button> `).join('')||'No networks found'}
async function saveWifiToggle(){await api('/api/config/wifi-toggle',{method:'POST',body:new URLSearchParams({enabled:$('#wifiEnabled').value})});loadStatus()}
async function loadSms(){let d=await api('/api/sms');$('#smsList').innerHTML=d.messages.map(m=>`<div class=msg><b>${esc(m.sender)}</b> · ${esc(m.timestamp)}<br>${esc(m.body)}</div>`).join('')||'<p>No archived messages.</p>'}
async function clearSms(){if(confirm('Delete archived SMS history?')){await api('/api/sms/clear',{method:'POST'});loadSms()}}
async function loadLogs(){let d=await api('/api/logs');$('#logs').innerHTML=d.events.map(e=>`<div class=msg><b class=${e.success?'ok':'bad'}>${esc(e.result)}</b> · ${esc(e.transport)} · ${e.durationMs} ms · ${e.bytes} bytes<br>${esc(e.endpoint)}<br><span class=muted>${esc(e.trigger)} ${esc(e.fallback)}</span></div>`).join('')||'<p>No events.</p>'}
async function clearLogs(){if(confirm('Clear phone-home history?')){await api('/api/logs/clear',{method:'POST'});loadLogs()}}
function originLabel(o){return o=='tmads'?'Fleet campaign':o=='upload'?'Local override':o||''}
async function loadMedia(){let d=await api('/api/media');$('#currentMeta').textContent=d.current.available?`${d.current.name} · ${d.current.orientation}° · ${originLabel(d.current.origin)}`:'Not captured yet';$('#nextMeta').textContent=d.next.available?`${d.next.name} · ${d.next.orientation}° · ${originLabel(d.next.origin)}`:'No media queued';
const q=deviceKey?`?key=${encodeURIComponent(deviceKey)}`:'';
document.querySelectorAll('#media img').forEach((img,i)=>{img.style.display='';img.src=(i==0?'/media/current.bmp':'/media/next.bmp')+q});
document.querySelectorAll('#media a.btn').forEach((a,i)=>{a.href=(i==0?'/media/current.bmp':'/media/next.bmp')+(deviceKey?`?download=1&key=${encodeURIComponent(deviceKey)}`:'?download=1')});
loadStatus()}
async function refreshDisplay(){if(confirm('Display Next now and queue Current for the next tap?')){await api('/api/media/refresh',{method:'POST'});alert('Refresh queued')}}
async function speed(t){await api('/api/bandwidth',{method:'POST',body:new URLSearchParams({transport:t,bytes:$('#speedBytes').value})});$('#speedResult').textContent='Test queued…';setTimeout(loadStatus,3000)}
loadStatus();setInterval(loadStatus,10000);
</script></body></html>
)HTML";

void sendJson(const JsonDocument &doc, int status = 200) {
    String body;
    serializeJson(doc, body);
    gServer.send(status, "application/json", body);
    activityBump();
}

void sendError(int status, const char *message) {
    JsonDocument doc;
    doc["error"] = message;
    sendJson(doc, status);
}

void handleStatus() {
    const auto config = device_config::getSnapshot();
    const auto modem = modemGetDiagnostics();
    JsonDocument doc;
    doc["ap"]["ssid"] = gSsid;
    doc["ap"]["ip"] = WiFi.softAPIP().toString();
    doc["csrfToken"] = gCsrfToken;
    if (requestFromSoftAp()) {
        // The device key is only revealed on the captive AP so LAN clients
        // cannot bootstrap themselves.
        doc["ap"]["deviceKey"] = gPortalKey;
    }
    doc["rail"] = powerRailIsOn();
    BatteryTelemetry battery;
    if (batteryRead(&battery)) {
        doc["battery"]["available"] = true;
        doc["battery"]["voltageV"] = battery.busVoltageV;
        doc["battery"]["currentMa"] = battery.currentMa;
        doc["battery"]["powerMw"] = battery.powerMw;
        doc["battery"]["socPercent"] = battery.socPct;
    } else {
        doc["battery"]["available"] = false;
        doc["battery"]["error"] = battery.error;
    }
    doc["uptimeMs"] = millis();
    doc["heapFree"] = ESP.getFreeHeap();
    doc["psramFree"] = ESP.getFreePsram();
    doc["flashSize"] = ESP.getFlashChipSize();
    doc["littlefsUsed"] = LittleFS.usedBytes();
    doc["littlefsTotal"] = LittleFS.totalBytes();
    const UsbCampaignStorageStatus usbStorage =
        usbCampaignStorageGetStatus();
    doc["usbStorage"]["hostReady"] = usbStorage.hostReady;
    doc["usbStorage"]["connected"] = usbStorage.connected;
    doc["usbStorage"]["mounted"] = usbStorage.mounted;
    doc["usbStorage"]["writable"] = usbStorage.writable;
    doc["usbStorage"]["vendorId"] = usbStorage.vendorId;
    doc["usbStorage"]["productId"] = usbStorage.productId;
    doc["usbStorage"]["product"] = usbStorage.product;
    doc["usbStorage"]["manufacturer"] = usbStorage.manufacturer;
    doc["usbStorage"]["serial"] = usbStorage.serial;
    doc["usbStorage"]["capacityBytes"] = usbStorage.capacityBytes;
    doc["usbStorage"]["totalBytes"] = usbStorage.totalBytes;
    doc["usbStorage"]["freeBytes"] = usbStorage.freeBytes;
    doc["usbStorage"]["sectorSize"] = usbStorage.sectorSize;
    doc["usbStorage"]["error"] = usbStorage.error;
    doc["wifi"]["connected"] = networkWifiConnected();
    doc["wifi"]["ssid"] = networkWifiSsid();
    doc["wifi"]["ip"] = networkWifiIp();
    doc["wifi"]["rssi"] = networkWifiRssi();
    doc["config"]["wifiEnabled"] = config.wifiEnabled;
    doc["config"]["autoApn"] = config.cellularAutoApn;
    doc["config"]["apn"] = config.cellularApn;
    doc["config"]["user"] = config.cellularUsername;
    doc["config"]["hasCellPassword"] = config.cellularPassword[0] != '\0';
    doc["config"]["hasSimPin"] = config.simPin[0] != '\0';
    doc["config"]["auth"] = device_config::toString(config.cellularAuth);
    doc["config"]["pdp"] = device_config::toString(config.cellularPdp);
    doc["config"]["roaming"] = config.roamingAllowed;
    doc["config"]["orientation"] =
        static_cast<uint16_t>(config.displayOrientation);
    JsonArray profiles = doc["config"]["profiles"].to<JsonArray>();
    for (const auto &profile : config.wifiProfiles) {
        JsonObject item = profiles.add<JsonObject>();
        item["enabled"] = profile.enabled;
        item["ssid"] = profile.ssid;
        item["hasPassword"] = profile.password[0] != '\0';
    }
    doc["smsCount"] = smsStoreCount();
    doc["phoneHomeCount"] = phone_home_log::count();
    doc["media"]["currentName"] = carouselCurrentImageName();
    addFrameJson(doc["media"]["current"].to<JsonObject>(),
                 FrameCacheSlot::Current);
    addFrameJson(doc["media"]["next"].to<JsonObject>(),
                 FrameCacheSlot::Next);
    doc["display"]["initialized"] = displayPanelInitialized();
    doc["display"]["lastError"] = displayLastError();
    const DisplayIdentity displayIdentity = displayIdentityGet();
    doc["display"]["eeprom"]["present"] = displayIdentity.present;
    doc["display"]["eeprom"]["width"] = displayIdentity.width;
    doc["display"]["eeprom"]["height"] = displayIdentity.height;
    doc["display"]["eeprom"]["color"] = displayIdentity.color;
    doc["display"]["eeprom"]["pcbVariant"] = displayIdentity.pcbVariant;
    doc["display"]["eeprom"]["displayVariant"] =
        displayIdentity.displayVariant;
    Preferences campaignPrefs;
    if (campaignPrefs.begin("tmads_campaign", true)) {
        doc["display"]["activeAssetHash"] =
            campaignPrefs.getString("active_hash", "");
        doc["display"]["lastCampaignError"] =
            campaignPrefs.getString("last_error", "");
        doc["display"]["lastCampaignAttemptSec"] =
            campaignPrefs.getUInt("last_attempt", 0);
        campaignPrefs.end();
    }
    tmads::CampaignCacheStatus campaignStatus;
    if (tmads::campaignCacheGetStatus(&campaignStatus)) {
        doc["campaignCache"]["ready"] = campaignStatus.ready;
        doc["campaignCache"]["cursor"] = campaignStatus.cursor;
        doc["campaignCache"]["assetCount"] = campaignStatus.assetCount;
        doc["campaignCache"]["cachedCount"] = campaignStatus.cachedCount;
        // LittleFS framebuffer usage and USB campaign bytes are reported
        // separately so storage pressure is attributable.
        doc["campaignCache"]["littlefsUsedBytes"] = campaignStatus.usedBytes;
        doc["campaignCache"]["littlefsTotalBytes"] = campaignStatus.totalBytes;
        doc["campaignCache"]["littlefsFreeBytes"] =
            campaignStatus.totalBytes - campaignStatus.usedBytes;
        doc["campaignCache"]["usbCampaignBytes"] =
            campaignStatus.usbCampaignBytes;
        doc["campaignCache"]["usbActive"] = campaignStatus.usbActive;
        JsonArray cachedAssets =
            doc["campaignCache"]["assets"].to<JsonArray>();
        for (size_t i = 0; i < tmads::campaignCatalogCount(); ++i) {
            tmads::CatalogAsset asset;
            if (!tmads::campaignCatalogGet(i, &asset)) continue;
            JsonObject item = cachedAssets.add<JsonObject>();
            item["assetHash"] = asset.assetHash;
            item["campaignId"] = asset.campaignId;
            item["mediaId"] = asset.mediaId;
            item["status"] = asset.campaignStatus;
            item["variant"] = asset.variant;
            item["bytes"] = asset.byteLength;
            item["cached"] = asset.cached;
            item["eligible"] = asset.eligible;
        }
    }
    doc["modem"]["ready"] = modem.ready;
    doc["modem"]["registered"] = modem.networkRegistered;
    doc["modem"]["dataConnected"] = modem.dataConnected;
    doc["modem"]["model"] = modem.model;
    doc["modem"]["imei"] = modem.imei;
    doc["modem"]["number"] = modem.phoneNumber;
    doc["modem"]["iccid"] = modem.iccid;
    doc["modem"]["imsi"] = modem.imsi;
    doc["modem"]["operator"] = modem.operatorName;
    doc["modem"]["signalQuality"] = modem.signalQuality;
    doc["modem"]["ip"] = modem.cellularIp;
    doc["gps"]["fixed"] = modem.gnssFixed;
    doc["gps"]["latitude"] = modem.latitude;
    doc["gps"]["longitude"] = modem.longitude;
    doc["gps"]["altitude"] = modem.altitude;
    doc["gps"]["satellites"] = modem.satellites;
    doc["action"] = gLastAction;
    doc["bandwidth"] = gBandwidthResult;
    sendJson(doc);
}

void handleWifiScan() {
    WifiNetworkInfo found[20] = {};
    const size_t count = networkScan(found, 20);
    JsonDocument doc;
    JsonArray networks = doc["networks"].to<JsonArray>();
    for (size_t i = 0; i < count; ++i) {
        JsonObject item = networks.add<JsonObject>();
        item["ssid"] = found[i].ssid;
        item["rssi"] = found[i].rssi;
        item["secured"] = found[i].secured;
    }
    sendJson(doc);
}

void handleCellularSave() {
    auto config = device_config::getSnapshot();
    config.cellularAutoApn = gServer.arg("autoApn") == "1";
    strncpy(config.cellularApn, gServer.arg("apn").c_str(),
            sizeof(config.cellularApn) - 1);
    strncpy(config.cellularUsername, gServer.arg("user").c_str(),
            sizeof(config.cellularUsername) - 1);
    if (gServer.arg("pass").length()) {
        strncpy(config.cellularPassword, gServer.arg("pass").c_str(),
                sizeof(config.cellularPassword) - 1);
    }
    if (gServer.hasArg("clearPass")) config.cellularPassword[0] = '\0';
    if (gServer.arg("pin").length()) {
        strncpy(config.simPin, gServer.arg("pin").c_str(),
                sizeof(config.simPin) - 1);
    }
    if (gServer.hasArg("clearPin")) config.simPin[0] = '\0';
    device_config::parseCellularAuth(gServer.arg("auth").c_str(),
                                     config.cellularAuth);
    device_config::parsePdpType(gServer.arg("pdp").c_str(),
                                config.cellularPdp);
    config.roamingAllowed = gServer.arg("roaming") == "1";
    if (!device_config::save(config)) return sendError(500, "save failed");
    gServer.send(200, "application/json", "{\"ok\":true}");
}

void handleWifiSave() {
    const int slot = gServer.arg("slot").toInt();
    if (slot < 0 || slot >= static_cast<int>(device_config::kMaxWifiProfiles)) {
        return sendError(400, "invalid profile slot");
    }
    auto config = device_config::getSnapshot();
    auto &profile = config.wifiProfiles[slot];
    strncpy(profile.ssid, gServer.arg("ssid").c_str(),
            sizeof(profile.ssid) - 1);
    if (gServer.arg("password").length()) {
        strncpy(profile.password, gServer.arg("password").c_str(),
                sizeof(profile.password) - 1);
    }
    if (gServer.hasArg("clearPassword")) profile.password[0] = '\0';
    profile.enabled = gServer.arg("enabled") == "1";
    if (!device_config::save(config)) return sendError(500, "save failed");
    gServer.send(200, "application/json", "{\"ok\":true}");
}

void handleWifiToggle() {
    auto config = device_config::getSnapshot();
    config.wifiEnabled = gServer.arg("enabled") == "1";
    if (!device_config::save(config)) return sendError(500, "save failed");
    if (!config.wifiEnabled) networkDisconnectStation();
    gServer.send(200, "application/json", "{\"ok\":true}");
}

void handleOrientationSave() {
    auto config = device_config::getSnapshot();
    const auto previous = config.displayOrientation;
    if (!device_config::parseDisplayOrientation(
            gServer.arg("orientation").c_str(), config.displayOrientation)) {
        return sendError(400, "invalid orientation");
    }
    if (!device_config::save(config)) return sendError(500, "save failed");
    if (config.displayOrientation != previous) {
        frameCacheRemoveSlot(FrameCacheSlot::Next);
    }
    gServer.send(200, "application/json", "{\"ok\":true}");
}

void handleSms() {
    JsonDocument doc;
    JsonArray messages = doc["messages"].to<JsonArray>();
    const size_t count = min(smsStoreCount(), static_cast<size_t>(30));
    for (size_t i = 0; i < count; ++i) {
        SmsMessage message = {};
        if (!smsStoreGetNewest(i, &message)) continue;
        JsonObject item = messages.add<JsonObject>();
        item["id"] = message.id;
        item["sender"] = message.sender;
        item["timestamp"] = message.timestamp;
        item["body"] = message.body;
        item["incomplete"] = message.incomplete;
    }
    doc["total"] = smsStoreCount();
    sendJson(doc);
}

void handleLogs() {
    JsonDocument doc;
    JsonArray events = doc["events"].to<JsonArray>();
    const size_t count =
        min(phone_home_log::count(), static_cast<size_t>(30));
    for (size_t i = 0; i < count; ++i) {
        phone_home_log::Event event = {};
        if (!phone_home_log::getNewest(i, event)) continue;
        JsonObject item = events.add<JsonObject>();
        item["id"] = event.id;
        item["timestamp"] = event.timestamp;
        item["trigger"] = event.trigger;
        item["endpoint"] = event.endpoint;
        item["transport"] = event.transport;
        item["fallback"] = event.fallbackSummary;
        item["result"] = event.result;
        item["status"] = event.httpStatus;
        item["bytes"] = event.byteCount;
        item["durationMs"] = event.durationMs;
        item["success"] = event.success;
    }
    doc["total"] = phone_home_log::count();
    sendJson(doc);
}

void addFrameJson(JsonObject object, FrameCacheSlot slot) {
    FrameCacheInfo info = {};
    object["available"] = frameCacheGetInfo(slot, &info);
    if (!info.available) return;
    object["name"] = info.filename;
    object["origin"] = info.origin;
    object["orientation"] = info.orientation;
    object["bytes"] = info.payloadLength;
    object["crc32"] = info.crc32;
}

void handleMediaInfo() {
    JsonDocument doc;
    addFrameJson(doc["current"].to<JsonObject>(), FrameCacheSlot::Current);
    addFrameJson(doc["next"].to<JsonObject>(), FrameCacheSlot::Next);
    sendJson(doc);
}

uint8_t packedPixel(const uint8_t *frame, int x, int y) {
    const size_t index = static_cast<size_t>(y) * 1200U + x;
    const uint8_t value = frame[index / 2];
    return (index & 1U) ? (value & 0x0F) : (value >> 4);
}

uint8_t logicalPixel(const uint8_t *frame, int lx, int ly, uint16_t rotation) {
    int x = lx, y = ly;
    if (rotation == 90) {
        x = ly; y = 1599 - lx;
    } else if (rotation == 180) {
        x = 1199 - lx; y = 1599 - ly;
    } else if (rotation == 270) {
        x = 1199 - ly; y = lx;
    }
    return packedPixel(frame, x, y);
}

void put16(uint8_t *p, uint16_t value) {
    p[0] = value & 0xFF; p[1] = value >> 8;
}
void put32(uint8_t *p, uint32_t value) {
    p[0] = value & 0xFF; p[1] = (value >> 8) & 0xFF;
    p[2] = (value >> 16) & 0xFF; p[3] = value >> 24;
}

void handleFrameDownload(FrameCacheSlot slot) {
    FrameCacheInfo info = {};
    uint8_t *frame = static_cast<uint8_t *>(
        heap_caps_malloc(FRAME_CACHE_PAYLOAD_BYTES, MALLOC_CAP_SPIRAM));
    if (!frame || !frameCacheLoadSlot(slot, frame,
                                      FRAME_CACHE_PAYLOAD_BYTES, &info)) {
        if (frame) heap_caps_free(frame);
        return gServer.send(404, "text/plain", "Media not available");
    }
    const int width =
        (info.orientation == 90 || info.orientation == 270) ? 1600 : 1200;
    const int height =
        (info.orientation == 90 || info.orientation == 270) ? 1200 : 1600;
    const uint32_t rowBytes = ((static_cast<uint32_t>(width) + 1U) / 2U + 3U) & ~3U;
    const uint32_t dataBytes = rowBytes * height;
    const uint32_t offset = 14 + 40 + 16 * 4;
    const uint32_t totalBytes = offset + dataBytes;
    uint8_t header[offset] = {};
    header[0] = 'B'; header[1] = 'M';
    put32(header + 2, totalBytes); put32(header + 10, offset);
    put32(header + 14, 40); put32(header + 18, width);
    put32(header + 22, height); put16(header + 26, 1);
    put16(header + 28, 4); put32(header + 34, dataBytes);
    put32(header + 46, 16);
    const uint8_t palette[16][3] = {
        {0,0,0},{255,255,255},{0,255,255},{0,0,255},
        {255,255,255},{255,0,0},{0,255,0},{255,255,255},
        {255,255,255},{255,255,255},{255,255,255},{255,255,255},
        {255,255,255},{255,255,255},{255,255,255},{255,255,255}};
    for (int i = 0; i < 16; ++i) {
        header[54 + i * 4] = palette[i][0];
        header[55 + i * 4] = palette[i][1];
        header[56 + i * 4] = palette[i][2];
    }
    if (gServer.hasArg("download")) {
        gServer.sendHeader("Content-Disposition",
                           slot == FrameCacheSlot::Current
                               ? "attachment; filename=\"current.bmp\""
                               : "attachment; filename=\"next.bmp\"");
    }
    gServer.setContentLength(totalBytes);
    gServer.send(200, "image/bmp", "");
    WiFiClient client = gServer.client();
    client.write(header, sizeof(header));
    uint8_t *row = static_cast<uint8_t *>(malloc(rowBytes));
    if (row) {
        for (int outY = height - 1; outY >= 0 && client.connected(); --outY) {
            memset(row, 0, rowBytes);
            for (int x = 0; x < width; x += 2) {
                const uint8_t high =
                    logicalPixel(frame, x, outY, info.orientation);
                const uint8_t low = x + 1 < width
                                        ? logicalPixel(frame, x + 1, outY,
                                                       info.orientation)
                                        : 0;
                row[x / 2] = (high << 4) | low;
            }
            client.write(row, rowBytes);
            delay(0);
        }
        free(row);
    }
    heap_caps_free(frame);
    activityBump();
}

void handleUploadChunk() {
    HTTPUpload &upload = gServer.upload();
    ActionLock lock;
    if (!lock.locked()) return;
    if (upload.status == UPLOAD_FILE_START) {
        if (gActions.upload) heap_caps_free(gActions.upload);
        gActions.upload = static_cast<uint8_t *>(
            heap_caps_malloc(kMaxUploadBytes, MALLOC_CAP_SPIRAM));
        gActions.uploadLength = 0;
        gActions.uploadReady = false;
        gActions.uploadCommitted = false;
        strncpy(gActions.uploadName, upload.filename.c_str(),
                sizeof(gActions.uploadName) - 1);
    } else if (upload.status == UPLOAD_FILE_WRITE && gActions.upload) {
        if (gActions.uploadLength + upload.currentSize <= kMaxUploadBytes) {
            memcpy(gActions.upload + gActions.uploadLength, upload.buf,
                   upload.currentSize);
            gActions.uploadLength += upload.currentSize;
        } else {
            heap_caps_free(gActions.upload);
            gActions.upload = nullptr;
            gActions.uploadLength = 0;
        }
    } else if (upload.status == UPLOAD_FILE_END && gActions.upload) {
        gActions.uploadReady = true;
    } else if (upload.status == UPLOAD_FILE_ABORTED) {
        if (gActions.upload) heap_caps_free(gActions.upload);
        gActions.upload = nullptr;
        gActions.uploadLength = 0;
        gActions.uploadReady = false;
        gActions.uploadCommitted = false;
    }
}

void handleUploadComplete() {
    ActionLock lock;
    if (!lock.locked() || !gActions.uploadReady) {
        return sendError(400, "upload failed or exceeds 3 MB");
    }
    gActions.uploadCommitted = true;
    gLastAction = "upload queued";
    gServer.send(202, "application/json", "{\"queued\":true}");
    activityBump();
}

void handleManualRefresh() {
    ActionLock lock;
    if (!lock.locked()) return sendError(503, "busy");
    gActions.manualRefresh = true;
    gLastAction = "display refresh queued";
    gServer.send(202, "application/json", "{\"queued\":true}");
}

void handleBandwidth() {
    const size_t bytes = static_cast<size_t>(gServer.arg("bytes").toInt());
    if (bytes < 262144 || bytes > 5U * 1024U * 1024U) {
        return sendError(400, "invalid test size");
    }
    ActionLock lock;
    if (!lock.locked()) return sendError(503, "busy");
    gActions.bandwidthReady = true;
    gActions.bandwidthCellular = gServer.arg("transport") == "cellular";
    gActions.bandwidthBytes = bytes;
    gBandwidthResult = "queued";
    gServer.send(202, "application/json", "{\"queued\":true}");
}

void redirectCaptive() {
    gServer.sendHeader("Location", String("http://") + WiFi.softAPIP().toString(),
                       true);
    gServer.send(302, "text/plain", "");
}

void handleWifiJoin() {
    const String ssid = gServer.arg("ssid");
    const String password = gServer.arg("password");
    if (ssid.isEmpty() || ssid.length() > device_config::kMaxSsidLength) {
        return sendError(400, "invalid ssid");
    }
    auto config = device_config::getSnapshot();
    int slot = -1;
    for (size_t i = 0; i < device_config::kMaxWifiProfiles; ++i) {
        if (ssid == config.wifiProfiles[i].ssid) {
            slot = static_cast<int>(i);
            break;
        }
    }
    if (slot < 0) {
        for (size_t i = 0; i < device_config::kMaxWifiProfiles; ++i) {
            if (config.wifiProfiles[i].ssid[0] == '\0') {
                slot = static_cast<int>(i);
                break;
            }
        }
    }
    if (slot < 0) slot = device_config::kMaxWifiProfiles - 1;
    auto &profile = config.wifiProfiles[slot];
    strncpy(profile.ssid, ssid.c_str(), sizeof(profile.ssid) - 1);
    profile.ssid[sizeof(profile.ssid) - 1] = '\0';
    if (password.length()) {
        strncpy(profile.password, password.c_str(),
                sizeof(profile.password) - 1);
        profile.password[sizeof(profile.password) - 1] = '\0';
    }
    profile.enabled = true;
    config.wifiEnabled = true;
    if (!device_config::save(config)) return sendError(500, "save failed");
    JsonDocument doc;
    doc["saved"] = true;
    doc["slot"] = slot;
    const bool connected = networkConnectSaved(20000);
    doc["connected"] = connected;
    if (connected) {
        doc["ip"] = networkWifiIp();
        doc["ssid"] = networkWifiSsid();
    }
    sendJson(doc);
}

void guarded(const char *path, HTTPMethod method,
             WebServer::THandlerFunction handler) {
    gServer.on(path, method, [handler, method]() {
        if (!portalAuthorized()) {
            return sendError(401, "device key required for LAN access");
        }
        if (method != HTTP_GET &&
            gServer.header("X-CSRF-Token") != gCsrfToken) {
            return sendError(403, "CSRF validation failed");
        }
        handler();
    });
}

void guardedUpload(const char *path, WebServer::THandlerFunction handler,
                   WebServer::THandlerFunction uploadHandler) {
    gServer.on(path, HTTP_POST, [handler]() {
        if (!portalAuthorized()) {
            return sendError(401, "device key required for LAN access");
        }
        if (gServer.header("X-CSRF-Token") != gCsrfToken) {
            return sendError(403, "CSRF validation failed");
        }
        handler();
    }, uploadHandler);
}

void registerRoutes() {
    const char *headers[] = {"X-Portal-Key", "X-CSRF-Token"};
    gServer.collectHeaders(headers, 2);
    gServer.on("/", HTTP_GET, []() {
        gServer.send_P(200, "text/html", kPortalHtml);
        activityBump();
    });
    guarded("/api/status", HTTP_GET, handleStatus);
    guarded("/api/wifi/scan", HTTP_GET, handleWifiScan);
    guarded("/api/wifi/join", HTTP_POST, handleWifiJoin);
    guarded("/api/config/cellular", HTTP_POST, handleCellularSave);
    guarded("/api/config/wifi", HTTP_POST, handleWifiSave);
    guarded("/api/config/wifi-toggle", HTTP_POST, handleWifiToggle);
    guarded("/api/config/orientation", HTTP_POST, handleOrientationSave);
    guarded("/api/sms", HTTP_GET, handleSms);
    guarded("/api/sms/clear", HTTP_POST, []() {
        if (smsStoreClear()) gServer.send(200, "application/json", "{\"ok\":true}");
        else sendError(500, "clear failed");
    });
    guarded("/api/logs", HTTP_GET, handleLogs);
    guarded("/api/logs/clear", HTTP_POST, []() {
        if (phone_home_log::clear()) gServer.send(200, "application/json", "{\"ok\":true}");
        else sendError(500, "clear failed");
    });
    guarded("/api/media", HTTP_GET, handleMediaInfo);
    guarded("/media/current.bmp", HTTP_GET,
            []() { handleFrameDownload(FrameCacheSlot::Current); });
    guarded("/media/next.bmp", HTTP_GET,
            []() { handleFrameDownload(FrameCacheSlot::Next); });
    guardedUpload("/api/media/upload", handleUploadComplete,
                  handleUploadChunk);
    guarded("/api/media/refresh", HTTP_POST, handleManualRefresh);
    guarded("/api/bandwidth", HTTP_POST, handleBandwidth);
    gServer.on("/generate_204", HTTP_ANY, redirectCaptive);
    gServer.on("/hotspot-detect.html", HTTP_ANY, redirectCaptive);
    gServer.on("/connecttest.txt", HTTP_ANY, redirectCaptive);
    gServer.onNotFound(redirectCaptive);
}

void portalTask(void *) {
    while (gRunning) {
        gDns.processNextRequest();
        gServer.handleClient();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    gServer.stop();
    gDns.stop();
    gTask = nullptr;
    vTaskDelete(nullptr);
}

}  // namespace

bool configPortalBegin() {
    if (gRunning) return true;
    if (!gActionMutex) gActionMutex = xSemaphoreCreateMutex();
    if (!gActionMutex || !networkManagerBegin()) return false;
    ensurePortalKey();
    char csrf[25] = {};
    snprintf(csrf, sizeof(csrf), "%08lX%08lX%08lX",
             static_cast<unsigned long>(esp_random()),
             static_cast<unsigned long>(esp_random()),
             static_cast<unsigned long>(esp_random()));
    gCsrfToken = csrf;
    Serial.printf("Config portal: LAN device key %s\n", gPortalKey.c_str());
    uint64_t mac = ESP.getEfuseMac();
    char suffix[7] = {};
    snprintf(suffix, sizeof(suffix), "%06llX",
             static_cast<unsigned long long>(mac & 0xFFFFFFULL));
    gSsid = String("EInk-Setup-") + suffix;
    if (!WiFi.softAP(gSsid.c_str())) return false;
    delay(100);
    gDns.start(kDnsPort, "*", WiFi.softAPIP());
    registerRoutes();
    gServer.begin();
    gRunning = true;
    if (xTaskCreatePinnedToCore(portalTask, "config-portal", 8192, nullptr, 1,
                                &gTask, 0) != pdPASS) {
        gRunning = false;
        return false;
    }
    Serial.printf("Config portal: open AP %s at http://%s\n", gSsid.c_str(),
                  WiFi.softAPIP().toString().c_str());
    return true;
}

void configPortalProcessActions() {
    uint8_t *upload = nullptr;
    size_t uploadLength = 0;
    char uploadName[96] = {};
    bool refresh = false;
    bool bandwidth = false;
    bool cellular = false;
    size_t bandwidthBytes = 0;
    {
        ActionLock lock;
        if (!lock.locked()) return;
        if (gActions.uploadReady && gActions.uploadCommitted) {
            upload = gActions.upload;
            uploadLength = gActions.uploadLength;
            strncpy(uploadName, gActions.uploadName, sizeof(uploadName) - 1);
            gActions.upload = nullptr;
            gActions.uploadLength = 0;
            gActions.uploadReady = false;
            gActions.uploadCommitted = false;
        }
        refresh = gActions.manualRefresh;
        gActions.manualRefresh = false;
        bandwidth = gActions.bandwidthReady;
        cellular = gActions.bandwidthCellular;
        bandwidthBytes = gActions.bandwidthBytes;
        gActions.bandwidthReady = false;
    }

    if (upload) {
        gLastAction = "normalizing upload";
        // Decode on a dedicated large-stack worker; JPEG/PNG decoding on the
        // Arduino loop stack has previously caused resets.
        struct UploadJob {
            uint8_t *data;
            size_t length;
            char name[96];
            bool stored;
            TaskHandle_t caller;
        } job = {upload, uploadLength, {}, false,
                 xTaskGetCurrentTaskHandle()};
        strncpy(job.name, uploadName, sizeof(job.name) - 1);
        auto worker = [](void *argument) {
            auto *work = static_cast<UploadJob *>(argument);
            uint8_t *frame = static_cast<uint8_t *>(heap_caps_malloc(
                FRAME_CACHE_PAYLOAD_BYTES, MALLOC_CAP_SPIRAM));
            size_t frameLength = 0;
            const uint16_t orientation = static_cast<uint16_t>(
                device_config::getSnapshot().displayOrientation);
            const bool decoded =
                frame && imageDecodeCropFill(work->data, work->length,
                                             work->name, frame,
                                             FRAME_CACHE_PAYLOAD_BYTES,
                                             &frameLength, orientation);
            work->stored =
                decoded && frameCacheStoreSlot(FrameCacheSlot::Next, frame,
                                               frameLength, work->name,
                                               orientation, "upload");
            if (frame) heap_caps_free(frame);
            xTaskNotifyGive(work->caller);
            vTaskDelete(nullptr);
        };
        TaskHandle_t task = nullptr;
        if (xTaskCreatePinnedToCore(worker, "portal-upload", 24576, &job, 1,
                                    &task, 0) == pdPASS) {
            if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(60000)) == 0) {
                vTaskDelete(task);
                job.stored = false;
            }
        }
        heap_caps_free(upload);
        gLastAction = job.stored ? "upload ready as next" : "upload failed";
        activityBump();
    }
    if (refresh) {
        gLastAction = "refreshing display";
        gLastAction = carouselDisplayCachedAndSwap()
                          ? "display refreshed; old current queued"
                          : "display refresh failed";
        activityBump();
    }
    if (bandwidth) {
        bool testOk = false;
        float measuredMbps = 0;
        uint32_t measuredDuration = 0;
        if (cellular) {
            testOk = modemBandwidthTest(bandwidthBytes, &measuredMbps,
                                        &measuredDuration);
            if (testOk) {
                gBandwidthResult =
                    String("Cellular ") + String(measuredMbps, 2) +
                    " Mbps (" + measuredDuration + " ms)";
            } else {
                gBandwidthResult = "Cellular bandwidth test failed";
            }
        } else {
            testOk = networkWifiBandwidthTest(bandwidthBytes, &measuredMbps,
                                               &measuredDuration);
            if (testOk) {
                gBandwidthResult = String("Wi-Fi ") + String(measuredMbps, 2) +
                                   " Mbps (" + measuredDuration + " ms)";
            } else {
                gBandwidthResult = "Wi-Fi bandwidth test failed";
            }
        }
        phone_home_log::Event event = {};
        event.uptimeMs = millis();
        const time_t now = time(nullptr);
        if (now > 1700000000) {
            struct tm utc = {};
            gmtime_r(&now, &utc);
            strftime(event.timestamp, sizeof(event.timestamp),
                     "%Y-%m-%dT%H:%M:%SZ", &utc);
        }
        strncpy(event.trigger, "bandwidth test",
                sizeof(event.trigger) - 1);
        snprintf(event.endpoint, sizeof(event.endpoint),
                 "https://speed.cloudflare.com/__down");
        strncpy(event.transport, cellular ? "cellular" : "wifi",
                sizeof(event.transport) - 1);
        strncpy(event.result, testOk ? "ok" : "test failed",
                sizeof(event.result) - 1);
        event.httpStatus = testOk ? 200 : 0;
        event.byteCount = testOk ? static_cast<uint32_t>(bandwidthBytes) : 0;
        event.durationMs = measuredDuration;
        event.success = testOk;
        phone_home_log::append(event);
        activityBump();
    }
}

void configPortalStop() {
    if (!gRunning) return;
    gRunning = false;
    for (int i = 0; i < 50 && gTask; ++i) delay(10);
    WiFi.softAPdisconnect(true);
}

String configPortalSsid() {
    return gSsid;
}

String configPortalIp() {
    return WiFi.softAPIP().toString();
}
