#include "config_portal.h"

#include "EPD_13in3e.h"
#include "cache/frame_cache.h"
#include "carousel/carousel.h"
#include "config/device_config.h"
#include "image/image_pipeline.h"
#include "log/phone_home_log.h"
#include "modem/modem.h"
#include "network/network_manager.h"
#include "power/power.h"
#include "sms/sms_store.h"

#include <ArduinoJson.h>
#include <DNSServer.h>
#include <LittleFS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <string.h>
#include <time.h>

namespace {

constexpr size_t kMaxUploadBytes = 3U * 1024U * 1024U;
constexpr uint16_t kDnsPort = 53;
WebServer gServer(80);
DNSServer gDns;
TaskHandle_t gTask = nullptr;
SemaphoreHandle_t gActionMutex = nullptr;
String gSsid;
bool gRunning = false;

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
<section id=wifi><h2>Wi-Fi</h2><div class=card><label><span>Global Wi-Fi</span><select id=wifiEnabled><option value=1>Enabled</option><option value=0>Disabled</option></select></label><p><button onclick="saveWifiToggle()">Save toggle</button> <button onclick="scanWifi()">Scan networks</button></p><div id=scan></div></div>
<form id=wifiForm class=card><div class=grid><label>Profile slot<select name=slot><option>0</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option><option>7</option></select></label><label>SSID<input name=ssid required maxlength=32></label><label>Password<input name=password type=password maxlength=64 placeholder="unchanged"></label><label>Enabled<select name=enabled><option value=1>Yes</option><option value=0>No</option></select></label><label><span><input name=clearPassword type=checkbox value=1> Clear saved password</span></label></div><button>Save network</button></form><div id=profiles></div></section>
<section id=sms><h2>SMS Inbox</h2><p><button onclick=loadSms()>Refresh</button> <button onclick=clearSms()>Clear archive</button></p><div id=smsList></div></section>
<section id=home><h2>Phone Home Events</h2><p><button onclick=loadLogs()>Refresh</button> <button onclick=clearLogs()>Clear log</button></p><div id=logs></div></section>
<section id=media><h2>Media</h2><div class=grid><div class=card><h3>Current</h3><div id=currentMeta></div><img src="/media/current.bmp" onerror="this.style.display='none'"><p><a class=btn href="/media/current.bmp?download=1">Download</a></p></div><div class=card><h3>Next</h3><div id=nextMeta></div><img src="/media/next.bmp" onerror="this.style.display='none'"><p><a class=btn href="/media/next.bmp?download=1">Download</a></p></div></div>
<form id=orientationForm class=card><label>Display orientation<select name=orientation><option value=0>Portrait</option><option value=90>Landscape clockwise</option><option value=180>Portrait inverted</option><option value=270>Landscape counterclockwise</option></select></label><button>Save orientation</button></form>
<form id=uploadForm class=card enctype=multipart/form-data><label>Upload JPEG or PNG as next<input name=media type=file accept=".jpg,.jpeg,.png,image/jpeg,image/png" required></label><button>Upload as Next</button></form>
<p><button onclick=refreshDisplay()>Refresh now and queue old current</button></p></section>
<section id=speed><h2>Bandwidth Test</h2><div class=card><label>Download size<select id=speedBytes><option value=262144>256 KB</option><option value=1048576 selected>1 MB</option><option value=5242880>5 MB</option></select></label><p><button onclick="speed('wifi')">Test Wi-Fi</button> <button onclick="speed('cellular')">Test cellular</button></p><div id=speedResult></div></div></section>
<section id=diag><h2>Diagnostics</h2><button onclick=loadStatus()>Refresh</button><pre id=diagnostics class=card></pre></section>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function tab(id){document.querySelectorAll('section').forEach(x=>x.classList.toggle('on',x.id==id));if(id=='sms')loadSms();if(id=='home')loadLogs();if(id=='media')loadMedia()}tab('cell');
async function api(url,opt){let r=await fetch(url,opt);let t=await r.text();if(!r.ok)throw Error(t);try{return JSON.parse(t)}catch{return t}}
function formPost(form,url){form.addEventListener('submit',async e=>{e.preventDefault();try{await api(url,{method:'POST',body:new URLSearchParams(new FormData(form))});await loadStatus();alert('Saved')}catch(x){alert(x)}})}
formPost($('#cellForm'),'/api/config/cellular');formPost($('#wifiForm'),'/api/config/wifi');formPost($('#orientationForm'),'/api/config/orientation');
$('#uploadForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/media/upload',{method:'POST',body:new FormData(e.target)});alert('Upload queued for processing');setTimeout(loadMedia,2500)}catch(x){alert(x)}};
async function loadStatus(){let d=await api('/api/status');$('#summary').innerHTML=`AP <b>${esc(d.ap.ssid)}</b> · ${esc(d.ap.ip)} · 5V ${d.rail?'ON':'OFF'} · ${esc(d.action)}`;$('#diagnostics').textContent=JSON.stringify(d,null,2);$('#wifiEnabled').value=d.config.wifiEnabled?1:0;Object.assign($('#cellForm').elements,{ });let f=$('#cellForm').elements;f.autoApn.value=d.config.autoApn?1:0;f.apn.value=d.config.apn;f.user.value=d.config.user;f.pdp.value=d.config.pdp;f.auth.value=d.config.auth;f.roaming.value=d.config.roaming?1:0;$('#orientationForm').elements.orientation.value=d.config.orientation;$('#profiles').innerHTML=d.config.profiles.map((p,i)=>`<div class=card>Slot ${i}: <b>${esc(p.ssid||'(empty)')}</b> ${p.enabled?'enabled':'disabled'} ${p.hasPassword?'(password saved)':''}</div>`).join('');$('#speedResult').textContent=d.bandwidth}
async function scanWifi(){let d=await api('/api/wifi/scan');$('#scan').innerHTML=d.networks.map(n=>`<button type=button onclick="document.querySelector('#wifiForm [name=ssid]').value=${JSON.stringify(n.ssid)}">${esc(n.ssid)} (${n.rssi} dBm${n.secured?', secured':''})</button> `).join('')}
async function saveWifiToggle(){await api('/api/config/wifi-toggle',{method:'POST',body:new URLSearchParams({enabled:$('#wifiEnabled').value})});loadStatus()}
async function loadSms(){let d=await api('/api/sms');$('#smsList').innerHTML=d.messages.map(m=>`<div class=msg><b>${esc(m.sender)}</b> · ${esc(m.timestamp)}<br>${esc(m.body)}</div>`).join('')||'<p>No archived messages.</p>'}
async function clearSms(){if(confirm('Delete archived SMS history?')){await api('/api/sms/clear',{method:'POST'});loadSms()}}
async function loadLogs(){let d=await api('/api/logs');$('#logs').innerHTML=d.events.map(e=>`<div class=msg><b class=${e.success?'ok':'bad'}>${esc(e.result)}</b> · ${esc(e.transport)} · ${e.durationMs} ms · ${e.bytes} bytes<br>${esc(e.endpoint)}<br><span class=muted>${esc(e.trigger)} ${esc(e.fallback)}</span></div>`).join('')||'<p>No events.</p>'}
async function clearLogs(){if(confirm('Clear phone-home history?')){await api('/api/logs/clear',{method:'POST'});loadLogs()}}
async function loadMedia(){let d=await api('/api/media');$('#currentMeta').textContent=d.current.available?`${d.current.name} · ${d.current.orientation}° · ${d.current.origin}`:'Not captured yet';$('#nextMeta').textContent=d.next.available?`${d.next.name} · ${d.next.orientation}° · ${d.next.origin}`:'No media queued'}
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
    doc["rail"] = powerRailIsOn();
    doc["uptimeMs"] = millis();
    doc["heapFree"] = ESP.getFreeHeap();
    doc["psramFree"] = ESP.getFreePsram();
    doc["flashSize"] = ESP.getFlashChipSize();
    doc["littlefsUsed"] = LittleFS.usedBytes();
    doc["littlefsTotal"] = LittleFS.totalBytes();
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

void registerRoutes() {
    gServer.on("/", HTTP_GET, []() {
        gServer.send_P(200, "text/html", kPortalHtml);
        activityBump();
    });
    gServer.on("/api/status", HTTP_GET, handleStatus);
    gServer.on("/api/wifi/scan", HTTP_GET, handleWifiScan);
    gServer.on("/api/config/cellular", HTTP_POST, handleCellularSave);
    gServer.on("/api/config/wifi", HTTP_POST, handleWifiSave);
    gServer.on("/api/config/wifi-toggle", HTTP_POST, handleWifiToggle);
    gServer.on("/api/config/orientation", HTTP_POST, handleOrientationSave);
    gServer.on("/api/sms", HTTP_GET, handleSms);
    gServer.on("/api/sms/clear", HTTP_POST, []() {
        if (smsStoreClear()) gServer.send(200, "application/json", "{\"ok\":true}");
        else sendError(500, "clear failed");
    });
    gServer.on("/api/logs", HTTP_GET, handleLogs);
    gServer.on("/api/logs/clear", HTTP_POST, []() {
        if (phone_home_log::clear()) gServer.send(200, "application/json", "{\"ok\":true}");
        else sendError(500, "clear failed");
    });
    gServer.on("/api/media", HTTP_GET, handleMediaInfo);
    gServer.on("/media/current.bmp", HTTP_GET,
               []() { handleFrameDownload(FrameCacheSlot::Current); });
    gServer.on("/media/next.bmp", HTTP_GET,
               []() { handleFrameDownload(FrameCacheSlot::Next); });
    gServer.on("/api/media/upload", HTTP_POST, handleUploadComplete,
               handleUploadChunk);
    gServer.on("/api/media/refresh", HTTP_POST, handleManualRefresh);
    gServer.on("/api/bandwidth", HTTP_POST, handleBandwidth);
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
        uint8_t *frame = static_cast<uint8_t *>(
            heap_caps_malloc(FRAME_CACHE_PAYLOAD_BYTES, MALLOC_CAP_SPIRAM));
        size_t frameLength = 0;
        const uint16_t orientation = static_cast<uint16_t>(
            device_config::getSnapshot().displayOrientation);
        const bool decoded =
            frame && imageDecodeCropFill(upload, uploadLength, uploadName,
                                         frame, FRAME_CACHE_PAYLOAD_BYTES,
                                         &frameLength, orientation);
        const bool stored =
            decoded && frameCacheStoreSlot(FrameCacheSlot::Next, frame,
                                           frameLength, uploadName,
                                           orientation, "upload");
        if (frame) heap_caps_free(frame);
        heap_caps_free(upload);
        gLastAction = stored ? "upload ready as next" : "upload failed";
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
