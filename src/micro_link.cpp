#include "micro_link.h"
#include "micro_games.h"
#include "agent_status.h"
#include "device_settings.h"
#include "micro_sound.h"
#include "micro_orientation.h"
#include "title_font.h"
#include <cmath>
#include <Arduino.h>
#include <algorithm>
#include <ArduinoJson.h>
#include <USB.h>
#include <USBHID.h>
#include <tusb.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <M5Unified.h>
#include <cstring>

// Arduino's report dispatcher is useful for diagnosing a parsed vendor map.
USBHIDDevice *tinyusb_get_device_by_report_id(uint8_t report_id);

namespace {
constexpr uint8_t reportId = 6;
const uint8_t descriptor[] = {
  0x06,0x00,0xff, 0x09,0x01, 0xa1,0x01, 0x85,reportId,
  0x15,0x00, 0x26,0xff,0x00, 0x75,0x08, 0x95,0x3f,
  0x09,0x02, 0x81,0x02, 0x95,0x3f, 0x09,0x03, 0x91,0x02, 0xc0
};
struct Frame { uint8_t data[63]; };
QueueHandle_t frames;
USBHID hid;
MicroUI::State model;
MicroUI::State previewModel;
bool lightingPreview=false;
bool hostSeen=false;
bool titlesReceived=false;
uint32_t titlesAt=0;
bool configReceived=false, keysReceived=false, ambientReceived=false;
bool chatReceived=false;
bool actionPressDelivered[7]={};
uint32_t configAt=0, rgbRequests=0, stateErrors=0;
uint32_t lightRequests=0, parseErrors=0, sentKeys=0;
bool captureInput=false;
uint32_t capturedInputs=0;
struct RxDiagnostics {
  uint32_t raw=0, output=0, feature=0, fallback=0, accepted=0, rejected=0, dropped=0;
  uint32_t rejectId=0, rejectSize=0, rejectChannel=0, rejectQueue=0;
  uint8_t instance=0, id=0, type=0, head[4]={};
  uint16_t length=0;
  uint8_t deviceId=0, deviceHead[4]={};
  uint16_t deviceLength=0;
};
RxDiagnostics rx;
portMUX_TYPE rxLock=portMUX_INITIALIZER_UNLOCKED;
struct Metrics {
  uint32_t frameLast=0, frameMax=0, frames=0;
  uint32_t inputLast=0, inputMax=0, inputs=0;
} metrics;

void rejectFrame(uint32_t RxDiagnostics::*reason) {
  portENTER_CRITICAL(&rxLock);
  ++rx.rejected; ++(rx.*reason);
  portEXIT_CRITICAL(&rxLock);
}
void queueFrame(uint8_t id,const uint8_t *data,uint16_t len) {
  portENTER_CRITICAL(&rxLock);
  rx.deviceId=id; rx.deviceLength=len;
  memset(rx.deviceHead,0,sizeof(rx.deviceHead));
  if (data) memcpy(rx.deviceHead,data,std::min<size_t>(len,sizeof(rx.deviceHead)));
  portEXIT_CRITICAL(&rxLock);
  // Control and interrupt OUT paths may carry an explicit report ID byte.
  if ((id==0 || id==reportId) && data && len>=3 && data[0]==reportId) {
    ++data; --len; id=reportId;
  }
  if (id!=reportId) { rejectFrame(&RxDiagnostics::rejectId); return; }
  if (!data || len<2 || len>63) { rejectFrame(&RxDiagnostics::rejectSize); return; }
  if (data[0]!=2) { rejectFrame(&RxDiagnostics::rejectChannel); return; }
  if (data[1]>61 || data[1]+2>len) { rejectFrame(&RxDiagnostics::rejectSize); return; }
  if (!frames) { rejectFrame(&RxDiagnostics::rejectQueue); return; }
  Frame f{}; memcpy(f.data,data,len);
  const bool queued=xQueueSend(frames,&f,0)==pdTRUE;
  portENTER_CRITICAL(&rxLock);
  if (queued) ++rx.accepted;
  else { ++rx.dropped; ++rx.rejected; ++rx.rejectQueue; }
  portEXIT_CRITICAL(&rxLock);
}
char lastMethod[64]="none";
class Device : public USBHIDDevice {
public:
  Device() { USBHID::addDevice(this,sizeof(descriptor)); }
  uint16_t _onGetDescriptor(uint8_t *buffer) override {
    memcpy(buffer,descriptor,sizeof(descriptor)); return sizeof(descriptor);
  }
  void _onSetFeature(uint8_t id,const uint8_t *data,uint16_t len) override {
    portENTER_CRITICAL(&rxLock); ++rx.feature; portEXIT_CRITICAL(&rxLock);
    queueFrame(id,data,len);
  }
  void _onOutput(uint8_t id,const uint8_t *data,uint16_t len) override {
    portENTER_CRITICAL(&rxLock); ++rx.output; portEXIT_CRITICAL(&rxLock);
    queueFrame(id,data,len);
  }
} device;
char json[4096];
size_t jsonLen=0;
int depth=0;
bool inString=false, escaped=false;
uint32_t rxAt=0;

bool transmit(const String &line) {
  if (!hid.ready()) return false;
  String out=line; out+='\n';
  for (size_t pos=0;pos<out.length();) {
    uint8_t data[63]={2,0}; size_t n=std::min<size_t>(61,out.length()-pos);
    data[1]=n; memcpy(data+2,out.c_str()+pos,n);
    if (!hid.SendReport(reportId,data,sizeof(data),100)) return false;
    pos+=n;
  }
  return true;
}
uint32_t color(JsonVariant v) {
  if (v.is<const char*>()) { const char* s=v.as<const char*>(); return strtoul(s+(*s=='#'),nullptr,16)&0xffffff; }
  return v.as<uint32_t>()&0xffffff;
}
bool readZone(JsonVariant value,HostFeedback::Zone& result) {
  if (!value.is<JsonObject>()) return false;
  JsonObject obj=value.as<JsonObject>();
  bool seen=false;
  if (obj.containsKey("c")) {
    if (!obj["c"].is<uint32_t>() || obj["c"].as<uint32_t>()>0xFFFFFF) return false;
    result.color=obj["c"].as<uint32_t>(); seen=true;
  }
  for (const char* name:{"e","m"}) if (obj.containsKey(name)) {
    if (!obj[name].is<uint8_t>()) return false;
    if (*name=='e') result.effect=obj[name].as<uint8_t>();
    else result.magic=obj[name].as<uint8_t>();
    seen=true;
  }
  for (const char* name:{"b","s"}) if (obj.containsKey(name)) {
    if (!obj[name].is<float>()) return false;
    const float number=obj[name].as<float>();
    if (!std::isfinite(number) || number<0 || number>1) return false;
    if (*name=='b') result.brightness=lroundf(number*255);
    else result.speed=lroundf(number*255);
    seen=true;
  }
  return seen;
}
bool readRgb(JsonVariant value,HostFeedback::Lights& result,bool& haveKeys,bool& haveAmbient) {
  if (!value.is<JsonObject>()) return false;
  JsonObject obj=value.as<JsonObject>();
  const bool keys=obj.containsKey("keys"), ambient=obj.containsKey("ambient");
  if (!keys && !ambient) return false;
  if (keys && !readZone(obj["keys"],result.keys)) return false;
  if (ambient && !readZone(obj["ambient"],result.ambient)) return false;
  // A first partial patch does not establish omitted fields. Only use
  // defaults after a complete baseline from the host has been received.
  const auto complete=[](JsonVariant zone) {
    JsonObject obj=zone.as<JsonObject>();
    return obj.containsKey("c") && obj.containsKey("b") && obj.containsKey("e")
        && obj.containsKey("s") && obj.containsKey("m");
  };
  haveKeys|=keys && complete(obj["keys"]);
  haveAmbient|=ambient && complete(obj["ambient"]);
  result.known=haveKeys && haveAmbient;
  return true;
}
template <typename T,size_t N>
bool readEnum(JsonVariant value,const char* const (&names)[N],T& result) {
  if (!value.is<const char*>()) return false;
  const char* input=value.as<const char*>();
  if (value.as<JsonString>().size()!=strlen(input)) return false;
  for (size_t i=0;i<N;++i) if (!strcmp(input,names[i])) {
    result=static_cast<T>(i); return true;
  }
  return false;
}
bool readConfig(JsonDocument& doc,HostFeedback::Config& result,bool& available) {
  if (!doc.is<JsonObject>() || !doc["v"].is<int>() || doc["v"].as<int>()!=1
      || !doc["seq"].is<uint32_t>() || !doc["source"].is<const char*>()) return false;
  const char* source=doc["source"].as<const char*>();
  if (doc["source"].as<JsonString>().size()!=strlen(source)) return false;
  available=!strcmp(source,"local-config");
  if (!available && strcmp(source,"unavailable")) return false;
  static const char* const tiers[]={"unknown","standard","fast","ultrafast"};
  static const char* const encoders[]={"unknown","composer-navigation","reasoning","conversation-scroll","custom"};
  static const char* const voices[]={"unknown","push-to-talk","realtime"};
  static const char* const actions[]={"unknown","none","fast","approve","reject","fork","voice","aux","send","plan","sidebar","back","forward","custom"};
  if (!readEnum(doc["tier"],tiers,result.defaultTier)
      || !readEnum(doc["encoder"],encoders,result.encoder)
      || !readEnum(doc["voice"],voices,result.voice)
      || !doc["actions"].is<JsonArray>() || doc["actions"].size()!=7) return false;
  for (int i=0;i<7;++i)
    if (!readEnum(doc["actions"][i],actions,result.actions[i])) return false;
  if (!available) {
    if (result.defaultTier!=HostFeedback::Tier::Unknown
        || result.encoder!=HostFeedback::Encoder::Unknown
        || result.voice!=HostFeedback::Voice::Unknown) return false;
    for (const auto action:result.actions) if (action!=HostFeedback::Action::Unknown) return false;
  }
  result.fresh=available;
  return true;
}
bool readChat(JsonDocument& doc,HostFeedback::Chat& result,bool& available) {
  HostFeedback::Chat next;
  available=false;
  // Older companions must clear live state rather than retain a stale target.
  if (!doc.containsKey("chat")) { result=next; return true; }
  if (!doc["chat"].is<JsonObject>()) return false;
  JsonObject item=doc["chat"].as<JsonObject>();
  if (!item["source"].is<const char*>() || !item["scope"].is<const char*>()
      || !item["thread_id"].is<const char*>() || !item["title"].is<const char*>()) return false;
  const char* source=item["source"].as<const char*>();
  if (!strcmp(source,"desktop-ipc")) available=true;
  else if (strcmp(source,"unavailable")) return false;
  if (strcmp(item["scope"].as<const char*>(),"explicit-target")) return false;
  static const char* const speeds[]={"unknown","default","standard","fast","ultrafast"};
  static const char* const modes[]={"unknown","default","plan"};
  static const char* const permissions[]={"unknown","full-access","workspace-write","read-only","custom"};
  if (!readEnum(item["speed"],speeds,next.speed)
      || !readEnum(item["mode"],modes,next.mode)) return false;
  // Older companions know speed/mode only; omission must stay unknown.
  if (item.containsKey("permission")
      && !readEnum(item["permission"],permissions,next.permission)) return false;
  const char* id=item["thread_id"].as<const char*>();
  const char* title=item["title"].as<const char*>();
  // JsonString lengths expose embedded NULs that C string comparisons cannot.
  if (item["source"].as<JsonString>().size()!=strlen(source)
      || item["scope"].as<JsonString>().size()!=strlen(item["scope"].as<const char*>())
      || item["thread_id"].as<JsonString>().size()!=strlen(id)
      || item["title"].as<JsonString>().size()!=strlen(title)
      || item["speed"].as<JsonString>().size()!=strlen(item["speed"].as<const char*>())
      || item["mode"].as<JsonString>().size()!=strlen(item["mode"].as<const char*>())) return false;
  if (!HostFeedback::validChatTitle(title)) return false;
  if (available) {
    if (!HostFeedback::validThreadId(id)) return false;
    next.source=HostFeedback::ChatSource::DesktopIpc;
    memcpy(next.thread_id,id,37);
    memcpy(next.title,title,strlen(title)+1);
  } else if (*id || *title || next.speed!=HostFeedback::Speed::Unknown
      || next.mode!=HostFeedback::Mode::Unknown
      || next.permission!=HostFeedback::Permission::Unknown) return false;
  next.fresh=available;
  result=next;
  return true;
}
void actionFeedback(const MicroUI::Event& event,bool sent) {
  if (!event.key || event.joystick || event.action>1 || strlen(event.key)!=5
      || strncmp(event.key,"ACT",3) || event.key[3]<'0' || event.key[3]>'1'
      || event.key[4]<'0' || event.key[4]>'9') return;
  const int index=(event.key[3]-'0')*10+event.key[4]-'0'-6;
  if (index<0 || index>=7) return;
  if (event.action==1) actionPressDelivered[index]=sent;
  model.actionFeedbackAt[index]=std::max<uint32_t>(1,millis());
  model.actionFeedbackSent[index]=sent && actionPressDelivered[index];
  if (event.action==0) actionPressDelivered[index]=false;
}
void process() {
  JsonDocument req;
  if (deserializeJson(req,json,jsonLen)) { ++parseErrors; return; }
  const char *method=req["method"]|"";
  if (!*method) return;
  strlcpy(lastMethod,method,sizeof(lastMethod));
  JsonDocument response;
  response["id"]=req["id"];
  if (!strcmp(method,"device.status")) {
    JsonObject r=response["result"].to<JsonObject>();
    r["version"]="tab5-micro-0.10.2"; r["profile_index"]=0; r["layer_index"]=0;
    int battery=M5.Power.getBatteryLevel();
    if (battery>=0 && battery<=100) r["battery"]=battery;
    r["is_charging"]=M5.Power.isCharging()==m5::Power_Class::is_charging_t::is_charging;
  } else if (!strcmp(method,"sys.version")) {
    response["result"]["version"]="tab5-micro-0.10.2";
  } else if (!strcmp(method,"v.oai.thstatus")) {
    for (JsonObject item:req["params"].as<JsonArray>()) {
      int i=item["id"]|-1; if (i<0 || i>5) continue;
      auto &slot=model.slots[i];
      if (!item["c"].isNull()) slot.color=color(item["c"]);
      if (!item["b"].isNull()) slot.brightness=constrain(item["b"].as<float>(),0.f,1.f)*255;
      if (!item["e"].isNull()) slot.effect=item["e"].as<uint8_t>();
      if (!item["s"].isNull()) slot.speed=constrain(item["s"].as<float>(),0.f,1.f)*255;
    }
    ++lightRequests; hostSeen=true; response["result"]=nullptr;
  } else if (!strcmp(method,"v.oai.rgbcfg")) {
    auto next=model.hostLights;
    bool keys=keysReceived,ambient=ambientReceived;
    if (readRgb(req["params"],next,keys,ambient)) {
      model.hostLights=next; keysReceived=keys; ambientReceived=ambient;
      ++rgbRequests; hostSeen=true; response["result"]=nullptr;
    } else {
      ++stateErrors; response["error"]["code"]=-32602;
      response["error"]["message"]="Invalid RGB state";
    }
  } else {
    response["error"]["code"]=-32601; response["error"]["message"]="Method not found";
  }
  ++model.rpc_count;
  if (!req["id"].isNull()) { String out; serializeJson(response,out); transmit(out); }
}
void reset() { jsonLen=0; depth=0; inString=false; escaped=false; }
void receive(uint8_t ch) {
  if (!jsonLen && ch!='{') return;
  if (jsonLen>=sizeof(json)-1) { ++parseErrors; reset(); return; }
  json[jsonLen++]=ch;
  if (inString) {
    if (escaped) escaped=false;
    else if (ch=='\\') escaped=true;
    else if (ch=='"') inString=false;
  } else {
    if (ch=='"') inString=true;
    else if (ch=='{' || ch=='[') ++depth;
    else if (ch=='}' || ch==']') --depth;
  }
  if (!depth) { json[jsonLen]=0; process(); reset(); }
}
}

// Called by the project-local Arduino glue before its report-ID lookup. The
// fallback handles only our report and only when Arduino has no route for it.
extern "C" bool tab5_hid_raw_report(uint8_t instance,uint8_t id,uint8_t type,
                                    const uint8_t *data,uint16_t len) {
  portENTER_CRITICAL(&rxLock);
  ++rx.raw; rx.instance=instance; rx.id=id; rx.type=type; rx.length=len;
  memset(rx.head,0,sizeof(rx.head));
  if (data) memcpy(rx.head,data,std::min<size_t>(len,sizeof(rx.head)));
  portEXIT_CRITICAL(&rxLock);
  if (tinyusb_get_device_by_report_id(reportId)!=nullptr) return false;
  const bool ours=id==reportId || (id==0 && data && len>=3 && data[0]==reportId);
  if (!ours || (type!=0 && type!=HID_REPORT_TYPE_OUTPUT && type!=HID_REPORT_TYPE_FEATURE)) return false;
  portENTER_CRITICAL(&rxLock); ++rx.fallback; portEXIT_CRITICAL(&rxLock);
  queueFrame(id,data,len);
  return true;
}

namespace MicroLink {
void begin() { frames=xQueueCreate(64,sizeof(Frame)); hid.begin(); }
void poll() {
  if (!tud_mounted()) {
    if (hostSeen) {
      for (int i=0;i<7;++i) {
        actionPressDelivered[i]=false;
        model.actionFeedbackAt[i]=0; model.actionFeedbackSent[i]=false;
      }
    }
    hostSeen=false; reset(); keysReceived=ambientReceived=false;
    configReceived=chatReceived=false;
    model.hostChat={};
    model.hostLights={};
  }
  if (!hostSeen || !titlesReceived || millis()-titlesAt>7500) model.titlesFresh=false;
  model.connected=tud_mounted()&&hostSeen;
  model.hostConfig.fresh=configReceived && model.connected && millis()-configAt<=7500;
  model.hostChat.fresh=chatReceived && model.connected && millis()-configAt<=7500;
  if (jsonLen && millis()-rxAt>2000) reset();
  Frame f;
  while (frames && xQueueReceive(frames,&f,0)==pdTRUE) {
    rxAt=millis(); for (int i=0;i<f.data[1];++i) receive(f.data[i+2]);
  }
}
void send(const MicroUI::Event &event) {
  if (!model.connected && !captureInput) { actionFeedback(event,false); return; }
  JsonDocument doc;
  if (event.joystick) { doc["m"]="v.oai.rad"; doc["p"]["a"]=event.angle; doc["p"]["d"]=event.distance; }
  else if (event.key) { doc["m"]="v.oai.hid"; doc["p"]["k"]=event.key; doc["p"]["act"]=event.action; }
  else return;
  String out; serializeJson(doc,out);
  if (captureInput) {
    ++capturedInputs;
    DebugSerial.print("INPUT_EVENT "); DebugSerial.println(out);
  } else {
    const bool sent=transmit(out);
    if (sent) ++sentKeys;
    actionFeedback(event,sent);
  }
}
void captureInputs(bool enabled) { captureInput=enabled; }
bool capturingInputs() { return captureInput; }
bool previewLights(const char *payload) {
  if (!captureInput) return false;
  JsonDocument doc;
  if (deserializeJson(doc,payload) || !doc.is<JsonArray>() || doc.size()!=6) return false;
  MicroUI::State next=model;
  next.connected=true; next.lightingPreview=true;
  const bool withTitles=doc[0].as<JsonObject>().containsKey("title");
  for (int i=0;i<6;++i) {
    JsonVariant item=doc[i];
    if (!item.is<JsonObject>() || !item["c"].is<uint32_t>()
        || !item["b"].is<float>() || !item["e"].is<uint8_t>()) return false;
    const float b=item["b"].as<float>(),s=item["s"]|0.f;
    if (!std::isfinite(b) || !std::isfinite(s) || b<0 || b>1 || s<0 || s>1) return false;
    next.slots[i].color=item["c"].as<uint32_t>();
    next.slots[i].brightness=b*255;
    next.slots[i].effect=item["e"].as<uint8_t>();
    next.slots[i].speed=s*255;
    if (item.as<JsonObject>().containsKey("title") != withTitles) return false;
    if (withTitles) {
      if (!item["title"].is<const char*>()) return false;
      const char* title=item["title"].as<const char*>();
      if (strlen(title)>=sizeof(next.slots[i].title)) return false;
      strlcpy(next.slots[i].title,title,sizeof(next.slots[i].title));
      snprintf(next.slots[i].identity,sizeof(next.slots[i].identity),"preview-%d",i);
    }
  }
  if (withTitles) next.titlesFresh=true;
  previewModel=next; lightingPreview=true;
  return true;
}
void clearLightingPreview() { lightingPreview=false; }
const MicroUI::State &state() { return lightingPreview ? previewModel : model; }
void recordFrame(uint32_t microseconds) {
  metrics.frameLast=microseconds;
  metrics.frameMax=std::max(metrics.frameMax,microseconds); ++metrics.frames;
}
void recordInputInterval(uint32_t microseconds) {
  metrics.inputLast=microseconds;
  metrics.inputMax=std::max(metrics.inputMax,microseconds); ++metrics.inputs;
}
void resetMetrics() { metrics=Metrics{}; MicroUI::resetRenderProfile(); }
bool applyTitles(const char *payload) {
  JsonDocument doc;
  if (deserializeJson(doc,payload) || doc["v"].as<int>()!=1
      || !doc["seq"].is<uint32_t>() || !doc["slots"].is<JsonArray>()) return false;
  const char *source=doc["source"]|"";
  bool available=!strcmp(source,"custom");
  if (!available && strcmp(source,"unavailable")) return false;
  JsonArray slots=doc["slots"].as<JsonArray>();
  if (slots.size()!=6) return false;
  MicroUI::State next=model;
  uint8_t seen=0;
  for (JsonVariant value:slots) {
    if (!value.is<JsonObject>()) return false;
    JsonObject item=value.as<JsonObject>();
    if (!item["id"].is<int>() || !item["key"].is<const char*>()
        || !item["title"].is<const char*>()) return false;
    int i=item["id"].as<int>();
    if (i<0 || i>=6 || (seen&(1<<i))) return false;
    seen|=1<<i;
    const char *key=item["key"].as<const char*>();
    const char *title=item["title"].as<const char*>();
    if (strlen(key)>=sizeof(next.slots[i].identity)) return false;
    if ((*key==0)!=(*title==0)) return false;
    if (!available && (*key || *title)) return false;
    // Keep UTF-8 codepoints intact when a title exceeds the on-device limit.
    size_t len=std::min(strlen(title),sizeof(next.slots[i].title)-1);
    if (title[len]) while (len && (uint8_t(title[len])&0xC0)==0x80) --len;
    memcpy(next.slots[i].title,title,len); next.slots[i].title[len]=0;
    for (size_t n=0;n<len;++n)
      if (uint8_t(next.slots[i].title[n])<32) next.slots[i].title[n]=' ';
    strlcpy(next.slots[i].identity,key,sizeof(next.slots[i].identity));
  }
  next.titlesFresh=available && hostSeen && tud_mounted();
  model=next; titlesReceived=available; titlesAt=millis();
  DebugSerial.printf("TITLES_OK %lu\n",(unsigned long)doc["seq"].as<uint32_t>());
  return true;
}
bool applyHostConfig(const char *payload) {
  JsonDocument doc;
  HostFeedback::Config next;
  HostFeedback::Chat nextChat;
  bool available=false,chatAvailable=false;
  if (deserializeJson(doc,payload) || !readConfig(doc,next,available)
      || !readChat(doc,nextChat,chatAvailable)) return false;
  next.fresh=available && hostSeen && tud_mounted();
  nextChat.fresh=chatAvailable && hostSeen && tud_mounted();
  model.hostConfig=next; configReceived=available; configAt=millis();
  model.hostChat=nextChat; chatReceived=chatAvailable;
  DebugSerial.printf("HOST_CONFIG_OK %lu\n",(unsigned long)doc["seq"].as<uint32_t>());
  return true;
}
bool previewHostState(const char *payload) {
  if (!captureInput) return false;
  JsonDocument doc;
  if (deserializeJson(doc,payload) || !doc.is<JsonObject>()) return false;
  auto next=lightingPreview ? previewModel : model;
  bool keys=false,ambient=false;
  if (!readRgb(doc["rgb"],next.hostLights,keys,ambient)) return false;
  JsonDocument config; config.set(doc["config"]);
  bool available=false,chatAvailable=false;
  if (!readConfig(config,next.hostConfig,available)
      || !readChat(config,next.hostChat,chatAvailable)) return false;
  if (doc.containsKey("feedback")) {
    JsonArray feedback=doc["feedback"].as<JsonArray>();
    if (!doc["feedback"].is<JsonArray>() || feedback.size()!=7) return false;
    for (int i=0;i<7;++i) {
      if (feedback[i].isNull()) {
        next.actionFeedbackAt[i]=0; next.actionFeedbackSent[i]=false;
      } else if (feedback[i].is<bool>()) {
        next.actionFeedbackAt[i]=std::max<uint32_t>(1,millis());
        next.actionFeedbackSent[i]=feedback[i].as<bool>();
      } else return false;
    }
  }
  next.connected=true; next.lightingPreview=true;
  previewModel=next; lightingPreview=true;
  return true;
}
void diagnostics() {
  RxDiagnostics snapshot;
  portENTER_CRITICAL(&rxLock); snapshot=rx; portEXIT_CRITICAL(&rxLock);
  JsonDocument d; d["firmware"]="tab5-micro-0.10.2"; d["usb_ready"]=hid.ready(); d["usb_mounted"]=tud_mounted();
  d["theme"]=MicroUI::theme()==MicroUI::Theme::Dark ? "dark" : "light";
  d["uptime_ms"]=millis();
  d["title_font"]["loaded"]=UiTitleFont::loaded();
  d["title_font"]["glyphs"]=UiTitleFont::glyphCount();
  d["title_font"]["asset_bytes"]=UiTitleFont::assetBytes();
  d["settings_open"]=MicroUI::settingsOpen();
  d["brightness_percent"]=MicroUI::brightness();
  d["brightness"]=DeviceSettings::backlight(MicroUI::brightness());
  d["settings_pending"]=DeviceSettings::pending();
  const auto audio=MicroSound::diagnostics();
  d["audio"]["available"]=audio.available; d["audio"]["enabled"]=audio.enabled;
  d["audio"]["running"]=audio.running; d["audio"]["volume"]=audio.volume;
  d["audio"]["master"]=audio.master;
  d["audio"]["queued"]=audio.queued; d["audio"]["played"]=audio.played;
  d["audio"]["released"]=audio.released; d["audio"]["dropped"]=audio.dropped;
  d["audio"]["suppressed"]=audio.suppressed;
  d["audio"]["enqueue_max_us"]=audio.enqueueMax; d["audio"]["start_max_us"]=audio.startMax;
  const auto orientation=MicroOrientation::diagnostics();
  d["orientation"]["imu_available"]=orientation.available;
  d["orientation"]["rotation"]=orientation.rotation; d["orientation"]["blocked"]=orientation.blocked;
  d["orientation"]["accel"]["x"]=orientation.ax; d["orientation"]["accel"]["y"]=orientation.ay;
  d["orientation"]["accel"]["z"]=orientation.az;
  d["orientation"]["gyro"]["x"]=orientation.gx; d["orientation"]["gyro"]["y"]=orientation.gy;
  d["orientation"]["gyro"]["z"]=orientation.gz;
  d["orientation"]["samples"]=orientation.samples; d["orientation"]["failures"]=orientation.failures;
  d["orientation"]["changes"]=orientation.changes;
  d["orientation"]["read_max_us"]=orientation.readMaxUs; d["orientation"]["sampled_at_ms"]=orientation.sampledAt;
  d["host_seen"]=hostSeen; d["rpc_count"]=model.rpc_count; d["lighting_requests"]=lightRequests;
  d["rgb_requests"]=rgbRequests; d["state_errors"]=stateErrors;
  const auto& displayState=state();
  d["host_feedback"]["config_fresh"]=displayState.hostConfig.fresh;
  d["host_feedback"]["config_age_ms"]=configReceived ? int32_t(millis()-configAt) : -1;
  d["host_feedback"]["default_tier"]=uint8_t(displayState.hostConfig.defaultTier);
  d["host_feedback"]["encoder"]=uint8_t(displayState.hostConfig.encoder);
  d["host_feedback"]["voice"]=uint8_t(displayState.hostConfig.voice);
  const auto& chat=displayState.hostChat;
  d["host_feedback"]["chat"]["fresh"]=chat.fresh;
  d["host_feedback"]["chat"]["source"]=chat.source==HostFeedback::ChatSource::DesktopIpc ? "desktop-ipc" : "unavailable";
  d["host_feedback"]["chat"]["scope"]="explicit-target";
  d["host_feedback"]["chat"]["thread_id"]=chat.thread_id;
  d["host_feedback"]["chat"]["title"]=chat.title;
  d["host_feedback"]["chat"]["speed"]=HostFeedback::speedName(chat.speed);
  d["host_feedback"]["chat"]["mode"]=HostFeedback::modeName(chat.mode);
  d["host_feedback"]["chat"]["permission"]=HostFeedback::permissionName(chat.permission);
  JsonArray actions=d["host_feedback"]["actions"].to<JsonArray>();
  for (auto action:displayState.hostConfig.actions) actions.add(uint8_t(action));
  d["host_feedback"]["rgb_known"]=displayState.hostLights.known;
  for (int i=0;i<2;++i) {
    const auto& zone=i ? displayState.hostLights.ambient : displayState.hostLights.keys;
    JsonObject obj=d["host_feedback"][i ? "ambient" : "keys"].to<JsonObject>();
    obj["c"]=zone.color; obj["b"]=zone.brightness; obj["e"]=zone.effect;
    obj["s"]=zone.speed; obj["m"]=zone.magic;
  }
  d["host_feedback"]["signal"]=HostFeedback::signalLabel(
      HostFeedback::classify(displayState.hostLights,displayState.connected));
  d["host_feedback"]["source"]="local-config-defaults; optional-explicit-chat-desktop-ipc";
  d["last_method"]=lastMethod; d["dropped"]=snapshot.dropped; d["parse_errors"]=parseErrors; d["inputs_tx"]=sentKeys;
  d["input_capture"]=captureInput; d["captured_inputs"]=capturedInputs;
  d["lighting_preview"]=lightingPreview; d["effects_frozen"]=MicroUI::effectsAreFrozen();
  d["titles_fresh"]=model.titlesFresh;
  int titleCount=0; for (const auto &slot:model.slots) if (*slot.title) ++titleCount;
  d["title_count"]=titleCount;
  const auto game=MicroGames::diagnostics();
  d["game"]=game.game; d["game_state"]=game.state;
  d["game_score"]=game.score; d["game_ticks"]=game.ticks;
  d["lookup_report6"]=tinyusb_get_device_by_report_id(reportId)==&device;
  d["hid_rx_raw"]=snapshot.raw; d["hid_rx_output"]=snapshot.output; d["hid_rx_feature"]=snapshot.feature;
  d["hid_rx_fallback"]=snapshot.fallback; d["hid_rx_accepted"]=snapshot.accepted; d["hid_rx_rejected"]=snapshot.rejected;
  d["hid_rx_reject_id"]=snapshot.rejectId; d["hid_rx_reject_size"]=snapshot.rejectSize;
  d["hid_rx_reject_channel"]=snapshot.rejectChannel; d["hid_rx_reject_queue"]=snapshot.rejectQueue;
  d["hid_rx_last_instance"]=snapshot.instance; d["hid_rx_last_id"]=snapshot.id;
  d["hid_rx_last_type"]=snapshot.type; d["hid_rx_last_len"]=snapshot.length;
  d["hid_rx_device_id"]=snapshot.deviceId; d["hid_rx_device_len"]=snapshot.deviceLength;
  for (int i=0;i<4;++i) { d["hid_rx_last_head"][i]=snapshot.head[i]; d["hid_rx_device_head"][i]=snapshot.deviceHead[i]; }
  d["render_last_us"]=metrics.frameLast; d["render_max_us"]=metrics.frameMax; d["render_samples"]=metrics.frames;
  d["input_interval_last_us"]=metrics.inputLast; d["input_interval_max_us"]=metrics.inputMax; d["input_interval_samples"]=metrics.inputs;
  const auto& ui=MicroUI::renderProfile();
  d["ui_profile"]["dial_restore_us"]=ui.dialRestore; d["ui_profile"]["dial_marks_us"]=ui.dialMarks;
  d["ui_profile"]["dial_arc_us"]=ui.dialArc; d["ui_profile"]["counter_restore_us"]=ui.counterRestore;
  d["ui_profile"]["counter_text_us"]=ui.counterText; d["ui_profile"]["dial_present_us"]=ui.dialPresent;
  d["ui_profile"]["writeback_us"]=ui.writeback;
  d["ui_profile"]["neon_draw_us"]=ui.neonDraw;
  d["ui_profile"]["neon_present_us"]=ui.neonPresent;
  d["ui_profile"]["neon_frames"]=ui.neonFrames;
  d["ui_profile"]["title_draw_us"]=ui.titleDraw;
  d["ui_profile"]["title_present_us"]=ui.titlePresent;
  d["ui_profile"]["title_frames"]=ui.titleFrames;
  for (uint8_t i=0;i<7;++i) {
    const auto title=MicroUI::titleScrollStatus(i);
    auto entry=d["title_scroll"][i].to<JsonObject>();
    entry["lines"]=title.lines; entry["visible_lines"]=title.visibleLines;
    entry["width"]=title.width; entry["height"]=title.height;
    entry["line_height"]=title.lineHeight; entry["offset"]=title.offset;
    entry["max_offset"]=title.maxOffset; entry["elapsed_ms"]=title.elapsed;
    entry["cycle_ms"]=title.cycle; entry["paused"]=title.paused;
    entry["frames"]=title.frames; entry["cached_bytes"]=title.cachedBytes;
    entry["ready"]=title.ready; entry["manual"]=title.manual;
  }
  d["display_width"]=M5.Display.width(); d["display_height"]=M5.Display.height();
  d["board_id"]=int(M5.getBoard()); d["touch_enabled"]=M5.Touch.isEnabled();
  d["psram"]=ESP.getPsramSize(); d["free_heap"]=ESP.getFreeHeap();
  const auto& visible=state();
  for (int i=0;i<6;++i) {
    const auto& slot=visible.slots[i];
    d["slots"][i]["c"]=slot.color; d["slots"][i]["b"]=slot.brightness;
    d["slots"][i]["e"]=slot.effect; d["slots"][i]["s"]=slot.speed;
    d["slots"][i]["label"]=AgentStatus::label(AgentStatus::classify(slot.color,slot.brightness,slot.effect,visible.connected));
  }
  serializeJson(d,DebugSerial); DebugSerial.println();
}
}
