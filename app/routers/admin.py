from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_db
from app.models import Device
from app.schemas import AgentSummary, MappingOut, MappingSuggestIn, MappingSuggestOut
from app.services import store
from app.services.device_hub import hub
from app.services.llm_mapper import suggest_rules_with_llm

router = APIRouter(tags=["admin"])


def _extract_names(raw: Any) -> list[str]:
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id") or item.get("value")
                if isinstance(name, str):
                    out.append(name)
        return out
    if isinstance(raw, dict):
        return [str(key) for key in raw.keys()]
    return []


def _extract_agent_taxonomy(profile: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    if not profile:
        return [], []
    emotions = _extract_names(profile.get("emotions", []))
    actions = _extract_names(profile.get("actions", []))
    return emotions, actions


@router.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>DeviceBridgeService Admin</title>
  <style>
    :root { --bg:#0b0f14; --panel:#111821; --text:#d8e2ef; --muted:#7f92a8; --accent:#1fb6ff; --ok:#25c281; --warn:#ffb020; --danger:#ff5d73; }
    *{box-sizing:border-box} body{margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;background:linear-gradient(145deg,#0b0f14,#0f1620);color:var(--text)}
    .wrap{max-width:1200px;margin:0 auto;padding:18px}
    h1,h2{margin:0 0 10px} h1{font-size:20px} h2{font-size:16px;color:#b9c8da}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    .panel{background:var(--panel);border:1px solid #1d2b3a;border-radius:12px;padding:12px}
    .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
    input,select,button,textarea{background:#0a1118;border:1px solid #26374a;color:var(--text);padding:8px;border-radius:8px}
    input,select{min-width:160px} button{cursor:pointer} button.primary{background:#12354a;border-color:#1f5b7a}
    button.good{background:#103b2b;border-color:#1f6b4f} button.bad{background:#4a1a23;border-color:#7a2f3f}
    table{width:100%;border-collapse:collapse;font-size:12px} th,td{padding:6px;border-bottom:1px solid #1d2b3a;text-align:left}
    .status{font-size:12px;color:var(--muted)} .ok{color:var(--ok)} .warn{color:var(--warn)} .danger{color:var(--danger)}
    textarea{width:100%;min-height:220px}
  </style>
</head>
<body>
<div class=\"wrap\">
  <h1>DeviceBridgeService Admin</h1>
  <div class=\"row\">
    <a href=\"/\" style=\"color:#7fd6ff\">Home</a>
    <a href=\"/health\" style=\"color:#7fd6ff\">Health</a>
    <a href=\"/docs\" style=\"color:#7fd6ff\">API Docs</a>
    <a href=\"/openapi.json\" style=\"color:#7fd6ff\">OpenAPI JSON</a>
  </div>
  <div class=\"status\" id=\"status\">Ready</div>

  <div class=\"grid\">
    <section class=\"panel\">
      <h2>Devices</h2>
      <div class=\"row\">
        <button onclick=\"refreshAll()\" class=\"primary\">Refresh</button>
        <button onclick=\"toggleSim()\" id=\"simBtn\">Connect Simulator</button>
      </div>
      <table id=\"devTable\"><thead><tr><th>ID</th><th>Name</th><th>Model</th><th>Online</th><th>Actions</th></tr></thead><tbody></tbody></table>
    </section>

    <section class=\"panel\">
      <h2>Add / Update Device</h2>
      <div class=\"row\"><input id=\"devId\" placeholder=\"device_id\" /><input id=\"devName\" placeholder=\"name\" /><input id=\"devModel\" placeholder=\"model\" /></div>
      <div class=\"row\"><input id=\"devFw\" placeholder=\"firmware\" value=\"0.1.0\" /><input id=\"devModes\" value=\"line,shape\" placeholder=\"render modes\" /><input id=\"devAnims\" value=\"neutral_blink,head_tilt,scan_sweep\" placeholder=\"animations\" /></div>
      <div class=\"row\"><input id=\"devAgent\" placeholder=\"default agent_id for PTT\" style=\"min-width:320px\" /></div>
      <div class=\"row\"><label><input type=\"checkbox\" id=\"devPassthrough\" /> accepts model directives directly</label></div>
      <div class=\"row\"><button onclick=\"saveDevice()\" class=\"good\">Save Device Capabilities</button></div>
    </section>

    <section class=\"panel\">
      <h2>Brain (Agent) + Mapping</h2>
      <div class=\"row\">
        <select id=\"agentSelect\"></select>
        <select id=\"deviceSelect\"></select>
        <select id=\"modeSelect\"><option>line</option><option>shape</option><option>photo_warp</option><option>model3d</option></select>
      </div>
      <div class=\"row\">
        <button onclick=\"loadMapping()\">Load Mapping</button>
        <button onclick=\"suggestMapping()\" class=\"primary\">Suggest Mapping</button>
        <button onclick=\"saveMapping()\" class=\"good\">Save Mapping</button>
      </div>
      <textarea id=\"mappingJson\"></textarea>
    </section>

    <section class=\"panel\">
      <h2>How It Works</h2>
      <p>1) Device connects via WS and declares capabilities.</p>
      <p>2) Select an agent brain and device.</p>
      <p>3) DBS suggests emotion/action -> animation mapping.</p>
      <p>4) Save mapping. Runtime translation then follows this profile.</p>
      <p>5) If device supports all model directives, mapping can be near-identity.</p>
      <p>6) Audio and mic paths are coordinated through session endpoints.</p>
    </section>

    <section class=\"panel\">
      <h2>Session Logs</h2>
      <div class=\"row\">
        <select id=\"sessionSelect\"></select>
        <button onclick=\"refreshSessions()\" class=\"primary\">Refresh Sessions</button>
        <button onclick=\"loadSessionEvents()\">Load Events</button>
        <button onclick=\"loadSessionFiles()\">Load Files</button>
      </div>
      <textarea id=\"eventsJson\" placeholder=\"Session events...\"></textarea>
      <textarea id=\"filesJson\" placeholder=\"Session files...\"></textarea>
    </section>
  </div>
</div>
<script>
let sim = null;
function setStatus(msg, cls='status'){const el=document.getElementById('status'); el.className='status '+cls; el.textContent=msg;}
function parseCSV(v){return v.split(',').map(x=>x.trim()).filter(Boolean)}

async function refreshDevices(){
  const res = await fetch('/api/devices'); const data = await res.json();
  const body = document.querySelector('#devTable tbody'); body.innerHTML='';
  const sel = document.getElementById('deviceSelect'); sel.innerHTML='';
  data.forEach(d=>{
    const tr=document.createElement('tr');
    tr.innerHTML = `<td>${d.device_id}</td><td>${d.name}</td><td>${d.model}</td><td class="${d.online?'ok':'warn'}">${d.online?'online':'offline'}</td>`+
      `<td><button onclick="forceDisconnect('${d.device_id}')" class="bad">Disconnect</button></td>`;
    body.appendChild(tr);
    const o=document.createElement('option'); o.value=d.device_id; o.textContent=`${d.device_id} (${d.online?'online':'offline'})`; sel.appendChild(o);
  });
}

async function refreshAgents(){
  const res = await fetch('/api/admin/agents'); const data = await res.json();
  const sel = document.getElementById('agentSelect'); sel.innerHTML='';
  data.forEach(a=>{const o=document.createElement('option'); o.value=a.agent_id; o.textContent=`${a.name} (${a.agent_id})`; sel.appendChild(o);});
}

async function refreshAll(){
  try { await Promise.all([refreshDevices(), refreshAgents()]); await refreshSessions(); setStatus('Refreshed', 'ok'); }
  catch(e){ setStatus('Refresh failed: '+e, 'danger'); }
}

async function saveDevice(){
  const device_id=document.getElementById('devId').value.trim();
  if(!device_id){setStatus('Device ID required', 'warn'); return;}
  const payload={
    name: document.getElementById('devName').value || 'Manual Device',
    model: document.getElementById('devModel').value || 'custom',
    firmware_version: document.getElementById('devFw').value || '0.1.0',
    capabilities: {
      render_modes: parseCSV(document.getElementById('devModes').value),
      animations: parseCSV(document.getElementById('devAnims').value),
      audio_codecs: ['wav'],
      sample_rates: [22050],
      mic_enabled: true,
      mic_format: 'pcm16',
      accepts_model_directives: document.getElementById('devPassthrough').checked,
      default_agent_id: (document.getElementById('devAgent').value || '').trim()
    }
  };
  const res=await fetch(`/api/devices/${device_id}/capabilities`, {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify(payload)});
  if(!res.ok){setStatus('Save device failed', 'danger'); return;}
  await refreshDevices(); await refreshSessions(); setStatus('Device saved', 'ok');
}

async function forceDisconnect(deviceId){
  const res=await fetch(`/api/admin/devices/${deviceId}/disconnect`, {method:'POST'});
  if(res.ok){setStatus(`Disconnected ${deviceId}`, 'ok'); await refreshDevices(); await refreshSessions();}
  else setStatus(`Disconnect failed for ${deviceId}`, 'danger');
}

async function loadMapping(){
  const device_id=document.getElementById('deviceSelect').value;
  const agent_id=document.getElementById('agentSelect').value;
  if(!device_id || !agent_id){setStatus('Select device and agent', 'warn'); return;}
  const res=await fetch(`/api/devices/${device_id}/mappings/${agent_id}`);
  const data=await res.json();
  document.getElementById('modeSelect').value = data.preferred_render_mode || 'line';
  document.getElementById('mappingJson').value = JSON.stringify(data, null, 2);
  setStatus('Mapping loaded', 'ok');
}

async function suggestMapping(){
  const device_id=document.getElementById('deviceSelect').value;
  const agent_id=document.getElementById('agentSelect').value;
  const preferred_render_mode=document.getElementById('modeSelect').value;
  const res=await fetch('/api/admin/mappings/suggest', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({agent_id, device_id, preferred_render_mode})});
  if(!res.ok){setStatus('Suggest failed', 'danger'); return;}
  const data=await res.json();
  document.getElementById('mappingJson').value = JSON.stringify(data, null, 2);
  setStatus('Mapping suggestion generated', 'ok');
}

async function saveMapping(){
  const device_id=document.getElementById('deviceSelect').value;
  if(!device_id){setStatus('Select device first', 'warn'); return;}
  const raw=JSON.parse(document.getElementById('mappingJson').value);
  const payload={
    agent_id: raw.agent_id,
    preferred_render_mode: raw.preferred_render_mode || document.getElementById('modeSelect').value,
    emotion_map: raw.emotion_map || {},
    action_map: raw.action_map || {}
  };
  const res=await fetch(`/api/devices/${device_id}/mappings`, {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify(payload)});
  if(!res.ok){setStatus('Save mapping failed', 'danger'); return;}
  setStatus('Mapping saved', 'ok');
}

async function toggleSim(){
  const btn=document.getElementById('simBtn');
  if(sim){sim.close(); sim=null; btn.textContent='Connect Simulator'; setStatus('Simulator disconnected', 'warn'); return;}
  const id=(document.getElementById('devId').value || 'browser-sim').trim();
  sim = new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+`/ws/device/${id}`);
  sim.onopen=()=>{
    sim.send(JSON.stringify({type:'hello', name:'Browser Simulator', model:'sim-browser', firmware_version:'0.0.1', capabilities:{render_modes:['line','shape'], animations:['neutral_blink','head_tilt','scan_sweep'], audio_codecs:['wav'], sample_rates:[22050], mic_enabled:true, mic_format:'pcm16'}}));
  };
  sim.onmessage=(evt)=>{
    const msg=JSON.parse(evt.data);
    if(msg.type==='hello.ack'){btn.textContent='Disconnect Simulator'; setStatus('Simulator connected', 'ok'); refreshDevices(); return;}
    if(msg.command_id){sim.send(JSON.stringify({type:'ack', command_id:msg.command_id, ok:true}));}
  };
  sim.onclose=()=>{sim=null; btn.textContent='Connect Simulator'; refreshDevices();};
}

async function refreshSessions(){
  const deviceId=document.getElementById('deviceSelect').value;
  if(!deviceId){return;}
  const res = await fetch(`/api/admin/devices/${deviceId}/sessions?limit=40`);
  if(!res.ok){return;}
  const data = await res.json();
  const sel = document.getElementById('sessionSelect');
  sel.innerHTML='';
  data.forEach(s=>{
    const o=document.createElement('option');
    o.value=s.session_id;
    o.textContent=`${s.session_id} ${s.active?'(active)':'(closed)'}`;
    sel.appendChild(o);
  });
}

async function loadSessionEvents(){
  const sid=document.getElementById('sessionSelect').value;
  if(!sid){setStatus('No session selected', 'warn'); return;}
  const res=await fetch(`/api/admin/sessions/${sid}/events?limit=300`);
  const data=await res.json();
  document.getElementById('eventsJson').value=JSON.stringify(data, null, 2);
}

async function loadSessionFiles(){
  const sid=document.getElementById('sessionSelect').value;
  if(!sid){setStatus('No session selected', 'warn'); return;}
  const res=await fetch(`/api/admin/sessions/${sid}/files`);
  const data=await res.json();
  document.getElementById('filesJson').value=JSON.stringify(data, null, 2);
}

refreshAll();
</script>
</body>
</html>"""


@router.get("/api/admin/agents", response_model=list[AgentSummary])
async def get_agents() -> list[AgentSummary]:
    url = f"{settings.agentmanager_url.rstrip('/')}/agents"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(url)
            res.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"agentmanager unavailable: {exc}") from exc
    data = res.json()
    out: list[AgentSummary] = []
    for item in data:
        out.append(
            AgentSummary(
                agent_id=item.get("agent_id", ""),
                name=item.get("name", "Unnamed"),
                profile=item.get("profile"),
                voice_enabled=bool(item.get("voice_enabled", False)),
                voice_config=item.get("voice_config") or None,
            )
        )
    return out


@router.post("/api/admin/devices/{device_id}/disconnect")
async def disconnect_device(device_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    disconnected = await hub.force_disconnect(device_id)
    row = await db.get(Device, device_id)
    if row is not None:
        row.online = False
        await db.commit()
    return {"status": "ok", "disconnected": disconnected}


@router.get("/api/admin/devices/{device_id}/sessions")
async def list_device_sessions(device_id: str, limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await store.list_bridge_sessions(db, device_id=device_id, limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "session_id": row.session_id,
                "device_id": row.device_id,
                "agent_id": row.agent_id,
                "upstream_session_id": row.upstream_session_id,
                "active": row.active,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            }
        )
    return out


@router.get("/api/admin/sessions/{session_id}/events")
async def list_session_events(session_id: str, limit: int = 400, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await store.list_session_events(db, session_id=session_id, limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row.id,
                "session_id": row.session_id,
                "event_type": row.event_type,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "payload": store.parse_event_payload(row.payload_json),
            }
        )
    return out


@router.get("/api/admin/sessions/{session_id}/files")
async def list_session_files(session_id: str) -> dict[str, Any]:
    root = Path("data/devices")
    if not root.exists():
        return {"session_id": session_id, "files": []}

    files: list[dict[str, Any]] = []
    for path in root.glob(f"*/sessions/{session_id}/**/*"):
        if not path.is_file():
            continue
        files.append(
            {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "modified_at": path.stat().st_mtime,
            }
        )
    files.sort(key=lambda x: x["path"])
    return {"session_id": session_id, "files": files}


@router.post("/api/admin/mappings/suggest", response_model=MappingSuggestOut)
async def suggest_mapping(payload: MappingSuggestIn, db: AsyncSession = Depends(get_db)) -> MappingSuggestOut:
    device = await db.get(Device, payload.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")

    agents = await get_agents()
    agent = next((a for a in agents if a.agent_id == payload.agent_id), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    caps = store.device_capabilities_dict(device)
    animations = [str(x) for x in caps.get("animations", ["neutral_blink"])]
    supported_modes = [str(x) for x in caps.get("render_modes", ["line"])]
    passthrough_model_tags = bool(caps.get("accepts_model_directives", False))

    emotions, actions = _extract_agent_taxonomy(agent.profile)
    preferred_render_mode = payload.preferred_render_mode or (supported_modes[0] if supported_modes else "line")

    emotion_map = await suggest_rules_with_llm(
        source_type="emotion",
        labels=emotions,
        animations=animations,
        supported_modes=supported_modes,
        preferred_render_mode=preferred_render_mode,
        passthrough_model_tags=passthrough_model_tags,
    )
    action_map = await suggest_rules_with_llm(
        source_type="action",
        labels=actions,
        animations=animations,
        supported_modes=supported_modes,
        preferred_render_mode=preferred_render_mode,
        passthrough_model_tags=passthrough_model_tags,
    )

    return MappingSuggestOut(
        agent_id=payload.agent_id,
        device_id=payload.device_id,
        preferred_render_mode=preferred_render_mode,
        emotion_map=emotion_map,
        action_map=action_map,
    )


@router.get("/api/admin/mappings/preview/{device_id}/{agent_id}", response_model=MappingOut)
async def preview_mapping(device_id: str, agent_id: str, db: AsyncSession = Depends(get_db)) -> MappingOut:
    row = await store.get_or_create_mapping(db, agent_id=agent_id, device_id=device_id)
    emotion_map, action_map = store.parse_mapping(row)
    return MappingOut(
        agent_id=row.agent_id,
        device_id=row.device_id,
        preferred_render_mode=row.preferred_render_mode,
        emotion_map=emotion_map,
        action_map=action_map,
    )
