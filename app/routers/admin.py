from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_db
from app.models import Device, TelemetrySample
from app.schemas import AgentSummary, MappingOut, MappingSuggestIn, MappingSuggestOut
from app.services import store
from app.services.device_hub import hub
from app.services.llm_mapper import suggest_rules_with_llm
from app.services.runtime import runtime

router = APIRouter(tags=["admin"])
DATA_ROOT = Path("data/devices")
FAKE_PREFIXES = ("browser-sim", "smoke-", "compat-smoke-", "admin-dev-", "disc-", "legacy-auto-validate-")


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


def _is_fake_device(device_id: str, name: str, model: str) -> bool:
    lowered = f"{device_id} {name} {model}".lower()
    return (
        device_id.startswith(FAKE_PREFIXES)
        or "simulator" in lowered
        or "sim-browser" in lowered
        or "smoke" in lowered
        or "compat" in lowered
        or "test" in lowered
    )


def _compact_payload(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if depth > 4:
        return "<truncated>"
    if isinstance(value, str):
        if key == "audio_base64":
            return f"<base64 {len(value)} chars>"
        if len(value) > 280:
            return f"{value[:280]}... <{len(value)} chars>"
        return value
    if isinstance(value, list):
        if key == "timeline" and len(value) > 24:
            sample = [_compact_payload(item, depth=depth + 1) for item in value[:12]]
            return {"count": len(value), "sample": sample}
        if len(value) > 24:
            return [_compact_payload(item, depth=depth + 1) for item in value[:12]] + [f"... {len(value) - 12} more"]
        return [_compact_payload(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {sub_key: _compact_payload(sub_value, key=sub_key, depth=depth + 1) for sub_key, sub_value in value.items()}
    return value


async def _service_health(name: str, url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            res = await client.get(url)
            return {"name": name, "ok": res.status_code == 200, "url": url, "status_code": res.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"name": name, "ok": False, "url": url, "error": str(exc)}


async def _latest_telemetry(db: AsyncSession, device_id: str) -> TelemetrySample | None:
    stmt = (
        select(TelemetrySample)
        .where(TelemetrySample.device_id == device_id)
        .order_by(TelemetrySample.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _resolve_agent_id(db: AsyncSession, device: Device) -> tuple[str, int]:
    caps = store.device_capabilities_dict(device)
    default_agent_id = str(caps.get("default_agent_id", "")).strip()
    mapping_rows = await store.list_device_mappings(db, device_id=device.device_id)
    if default_agent_id:
        return default_agent_id, len(mapping_rows)
    if len(mapping_rows) == 1:
        return str(mapping_rows[0].agent_id), 1
    return "", len(mapping_rows)


def _timeline_summary_from_events(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    out = {"emotions": [], "actions": [], "visemes": []}
    for event in reversed(events):
        if event["event_type"] != "agent.timeline":
            continue
        for item in event["payload"].get("timeline", []):
            kind = str(item.get("type", ""))
            value = str(item.get("value", ""))
            if kind == "emotion" and value and value not in out["emotions"]:
                out["emotions"].append(value)
            if kind == "action" and value and value not in out["actions"]:
                out["actions"].append(value)
            if kind == "viseme" and value and value not in out["visemes"]:
                out["visemes"].append(value)
        break
    return out


def _session_files(session_id: str) -> list[dict[str, Any]]:
    if not DATA_ROOT.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in DATA_ROOT.glob(f"*/sessions/{session_id}/**/*"):
        if not path.is_file():
            continue
        files.append(
            {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "modified_at": path.stat().st_mtime,
                "download_url": f"/api/admin/files/download?path={quote(str(path))}",
            }
        )
    files.sort(key=lambda item: item["path"])
    return files


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@router.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>DeviceBridgeService Control</title>
  <style>
    :root{
      --bg:#071019;--panel:#0d1a26;--panel-2:#122332;--line:#21435f;--text:#d9e8f4;--muted:#8ea6bb;
      --ok:#41d392;--warn:#f4c15d;--bad:#ff6c7d;--accent:#74d8ff;--accent-2:#ff9f5a;
    }
    *{box-sizing:border-box}
    body{margin:0;background:radial-gradient(circle at top,#13314a 0,#08111a 45%,#050a10 100%);color:var(--text);font:14px/1.45 ui-monospace,Menlo,Consolas,monospace}
    .wrap{max-width:1480px;margin:0 auto;padding:20px}
    .topbar,.toolbar,.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .topbar{justify-content:space-between;margin-bottom:18px}
    .title{font-size:28px;font-weight:700;letter-spacing:.04em}
    .subtitle{color:var(--muted)}
    a,button{transition:.15s ease}
    a.link,button{border:1px solid var(--line);background:var(--panel-2);color:var(--text);padding:10px 12px;border-radius:12px;text-decoration:none;cursor:pointer}
    button.primary{background:#0f3850;border-color:#2a718f;color:#d9f5ff}
    button.good{background:#123d2d;border-color:#2f7f5b}
    button.bad{background:#421a22;border-color:#7e3342}
    button:disabled{opacity:.5;cursor:not-allowed}
    .grid{display:grid;grid-template-columns:340px minmax(480px,1fr) 420px;gap:14px}
    .panel{background:linear-gradient(180deg,rgba(18,35,50,.95),rgba(10,20,30,.98));border:1px solid rgba(116,216,255,.15);border-radius:18px;padding:16px;box-shadow:0 20px 60px rgba(0,0,0,.28)}
    .panel h2,.panel h3{margin:0 0 12px}
    .panel h2{font-size:18px}
    .panel h3{font-size:15px;color:#cfe0ee}
    .cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}
    .card{background:rgba(8,18,28,.7);border:1px solid rgba(116,216,255,.12);border-radius:14px;padding:12px}
    .metric{font-size:24px;font-weight:700}
    .muted{color:var(--muted)}
    .pill{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border-radius:999px;border:1px solid var(--line);background:#09121a}
    .dot{width:9px;height:9px;border-radius:50%}
    .ok{background:var(--ok)} .warn{background:var(--warn)} .bad{background:var(--bad)}
    .list{display:flex;flex-direction:column;gap:8px;max-height:920px;overflow:auto;padding-right:4px}
    .device{padding:12px;border:1px solid transparent;border-radius:14px;background:#0a131d;cursor:pointer}
    .device.active{border-color:var(--accent);box-shadow:0 0 0 1px rgba(116,216,255,.35) inset}
    .device.offline{opacity:.72}
    .device .name{font-weight:700}
    .device .meta{color:var(--muted);font-size:12px}
    .kv{display:grid;grid-template-columns:150px 1fr;gap:6px 12px;font-size:13px}
    .kv div:nth-child(odd){color:var(--muted)}
    .topology{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;align-items:center}
    .node{position:relative;padding:16px;border-radius:18px;border:1px solid var(--line);background:#091521;min-height:112px}
    .node.live{box-shadow:0 0 0 1px rgba(65,211,146,.4) inset,0 0 36px rgba(65,211,146,.08)}
    .node.hot{box-shadow:0 0 0 1px rgba(244,193,93,.45) inset,0 0 36px rgba(244,193,93,.08)}
    .flow{display:flex;justify-content:center;align-items:center;color:var(--muted);font-size:20px}
    .timeline-strip{display:flex;gap:8px;flex-wrap:wrap}
    .tag{padding:6px 10px;border-radius:999px;background:#0b1520;border:1px solid #24455f;font-size:12px}
    .tag.hot{border-color:#e29552;color:#ffd1ab}
    .tag.good{border-color:#34885f;color:#9ff2c8}
    .scroll{max-height:280px;overflow:auto;border:1px solid rgba(116,216,255,.12);border-radius:14px;background:#08111a}
    .event{padding:10px 12px;border-bottom:1px solid rgba(116,216,255,.08)}
    .event:last-child{border-bottom:none}
    .event strong{display:block;margin-bottom:4px}
    .event pre{margin:0;white-space:pre-wrap;word-break:break-word;color:#abc0d1}
    .formgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    input,select,textarea{width:100%;background:#08111a;border:1px solid #23445e;color:var(--text);padding:10px 12px;border-radius:12px}
    textarea{min-height:220px;resize:vertical}
    .section{display:flex;flex-direction:column;gap:12px}
    .files a{display:block;color:var(--accent);text-decoration:none;padding:7px 0;border-bottom:1px solid rgba(116,216,255,.08)}
    .files a:last-child{border-bottom:none}
    .error{color:#ffb4c0}
    @media (max-width:1320px){.grid{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <div class="title">DeviceBridgeService Control</div>
        <div class="subtitle">Real devices, routing, live session flow, and artifacts in one panel.</div>
      </div>
      <div class="toolbar">
        <a class="link" href="/health" target="_blank">Health</a>
        <a class="link" href="/docs" target="_blank">Docs</a>
        <a class="link" href="/openapi.json" target="_blank">OpenAPI</a>
        <button class="primary" onclick="refreshAll()">Refresh</button>
        <button class="bad" onclick="cleanupFakeDevices()">Delete Fake Devices</button>
        <button class="bad" onclick="pruneStaleDevices()">Prune Offline Stale</button>
      </div>
    </div>

    <div class="cards">
      <div class="card"><div class="muted">Online Devices</div><div class="metric" id="metricOnline">0</div></div>
      <div class="card"><div class="muted">Active Sessions</div><div class="metric" id="metricSessions">0</div></div>
      <div class="card"><div class="muted">Selected Brain</div><div class="metric" id="metricBrain" style="font-size:18px">None</div></div>
      <div class="card"><div class="muted">Last Error</div><div class="metric" id="metricError" style="font-size:14px">None</div></div>
    </div>

    <div class="grid">
      <section class="panel section">
        <h2>Devices</h2>
        <div class="muted">Fake simulators are hidden by default and can be removed from the database.</div>
        <div id="deviceList" class="list"></div>
      </section>

      <section class="panel section">
        <h2>Live Topology</h2>
        <div id="servicePills" class="row"></div>
        <div class="topology">
          <div id="nodeDevice" class="node"><h3>Device</h3><div id="nodeDeviceBody" class="muted">No device selected</div></div>
          <div class="flow">→</div>
          <div id="nodeDBS" class="node live"><h3>DBS</h3><div id="nodeDBSBody" class="muted">Session broker, mapping, transport</div></div>
          <div id="nodeVoice" class="node"><h3>VoiceService</h3><div id="nodeVoiceBody" class="muted">STT/TTS path</div></div>
          <div class="flow">↗</div>
          <div id="nodeAgent" class="node"><h3>AgentManager</h3><div id="nodeAgentBody" class="muted">Brain session</div></div>
          <div id="nodeMap" class="node"><h3>Mapper</h3><div id="nodeMapBody" class="muted">Emotion/action translation</div></div>
          <div class="flow">↘</div>
          <div id="nodeAI" class="node"><h3>AIGateway</h3><div id="nodeAIBody" class="muted">LLM mapping on miss</div></div>
        </div>
        <div>
          <h3>Current Signals</h3>
          <div id="signalStrip" class="timeline-strip"></div>
        </div>
        <div>
          <h3>Recent Flow</h3>
          <div id="eventStream" class="scroll"></div>
        </div>
      </section>

      <section class="panel section">
        <h2>Routing And Mapping</h2>
        <div class="formgrid">
          <div>
            <label class="muted">Device</label>
            <input id="deviceId" readonly />
          </div>
          <div>
            <label class="muted">Brain</label>
            <select id="agentSelect"></select>
          </div>
          <div>
            <label class="muted">Preferred Render Mode</label>
            <select id="modeSelect">
              <option value="line">line</option>
              <option value="shape">shape</option>
              <option value="photo_warp">photo_warp</option>
              <option value="model3d">model3d</option>
            </select>
          </div>
          <div>
            <label class="muted">Actions</label>
            <div class="row">
              <button onclick="saveBrain()" class="good">Save Brain</button>
              <button onclick="loadMapping()">Load</button>
              <button onclick="suggestMapping()" class="primary">Suggest</button>
              <button onclick="saveMapping()" class="good">Save Mapping</button>
            </div>
          </div>
        </div>
        <div id="deviceFacts" class="kv"></div>
        <textarea id="mappingJson" placeholder="Mapping JSON"></textarea>

        <h3>Sessions And Files</h3>
        <div class="row">
          <select id="sessionSelect"></select>
          <button onclick="loadSessionArtifacts()">Load Session</button>
          <button onclick="disconnectSelected()" class="bad">Disconnect Device</button>
          <button onclick="deleteSelectedDevice()" class="bad">Delete Device Record</button>
        </div>
        <div id="sessionSummary" class="muted">No session selected.</div>
        <div id="filesLinks" class="files scroll"></div>
      </section>
    </div>
  </div>
<script>
let dashboard = null;
let selectedDeviceId = '';
let selectedSessionId = '';
const statusMap = {true:'ok', false:'bad'};

function byId(id){ return document.getElementById(id); }
function esc(v){ return String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtDate(v){ if(!v) return 'n/a'; try{return new Date(v).toLocaleString();}catch{return v;} }
function short(v,n=10){ v=String(v||''); return v.length>n ? v.slice(0,n)+'…' : v; }

async function refreshAll(keepSelection=true){
  const res = await fetch('/api/admin/dashboard');
  dashboard = await res.json();
  if(!keepSelection || !selectedDeviceId || !dashboard.devices.find(d => d.device_id === selectedDeviceId)){
    selectedDeviceId = dashboard.devices[0]?.device_id || '';
  }
  renderDashboard();
}

function renderDashboard(){
  byId('metricOnline').textContent = dashboard.devices.filter(d=>d.online).length;
  byId('metricSessions').textContent = dashboard.devices.filter(d=>d.latest_session && d.latest_session.active).length;
  const selected = dashboard.devices.find(d => d.device_id === selectedDeviceId) || null;
  byId('metricBrain').textContent = selected?.resolved_agent_name || selected?.resolved_agent_id || 'None';
  byId('metricError').textContent = selected?.latest_session?.last_error || 'None';

  const pillBox = byId('servicePills');
  pillBox.innerHTML = dashboard.services.map(s => `<span class="pill"><span class="dot ${s.ok?'ok':'bad'}"></span>${esc(s.name)}</span>`).join('');

  const deviceList = byId('deviceList');
  deviceList.innerHTML = '';
  dashboard.devices.forEach(device => {
    const el = document.createElement('div');
    el.className = 'device ' + (device.device_id === selectedDeviceId ? 'active ' : '') + (device.online ? '' : 'offline');
    el.onclick = () => { selectedDeviceId = device.device_id; renderDashboard(); };
    el.innerHTML = `
      <div class="name">${esc(device.name)}</div>
      <div class="meta">${esc(device.device_id)}</div>
      <div class="row">
        <span class="pill"><span class="dot ${device.online?'ok':'warn'}"></span>${device.online?'online':'offline'}</span>
        <span class="pill">${device.listening?'listening':'idle'}</span>
        <span class="pill">maps ${device.mapping_count}</span>
      </div>
      <div class="meta">brain ${esc(device.resolved_agent_name || device.resolved_agent_id || 'unassigned')}</div>
      <div class="meta">last seen ${esc(device.last_seen_human || 'never')}</div>
    `;
    deviceList.appendChild(el);
  });

  renderSelected(selected);
}

function renderSelected(device){
  byId('deviceId').value = device?.device_id || '';
  const agentSelect = byId('agentSelect');
  agentSelect.innerHTML = '<option value="">Select brain</option>' + dashboard.agents.map(agent => {
    const sel = agent.agent_id === (device?.resolved_agent_id || '') ? 'selected' : '';
    return `<option ${sel} value="${esc(agent.agent_id)}">${esc(agent.name)} (${esc(short(agent.agent_id,8))})</option>`;
  }).join('');

  if(!device){
    byId('nodeDeviceBody').innerHTML = 'No real device selected';
    byId('nodeAgentBody').innerHTML = 'No routing available';
    byId('nodeVoiceBody').innerHTML = 'No session';
    byId('nodeAIBody').innerHTML = 'No selection';
    byId('nodeMapBody').innerHTML = 'No selection';
    byId('deviceFacts').innerHTML = '';
    byId('signalStrip').innerHTML = '';
    byId('eventStream').innerHTML = '';
    byId('sessionSelect').innerHTML = '';
    byId('filesLinks').innerHTML = '';
    byId('sessionSummary').textContent = 'No session selected.';
    return;
  }

  byId('modeSelect').value = device.preferred_render_mode || 'line';
  byId('deviceFacts').innerHTML = `
    <div>Model</div><div>${esc(device.model)}</div>
    <div>Firmware</div><div>${esc(device.firmware_version)}</div>
    <div>Default Brain</div><div>${esc(device.default_agent_id || 'none')}</div>
    <div>Resolved Brain</div><div>${esc(device.resolved_agent_name || device.resolved_agent_id || 'none')}</div>
    <div>Animations</div><div>${esc((device.animations || []).join(', '))}</div>
    <div>Render Modes</div><div>${esc((device.render_modes || []).join(', '))}</div>
    <div>Last Error</div><div class="${device.latest_session?.last_error ? 'error' : ''}">${esc(device.latest_session?.last_error || 'none')}</div>
  `;

  const hot = device.listening || !!device.latest_session?.active;
  byId('nodeDevice').className = 'node ' + (device.online ? 'live ' : '') + (device.listening ? 'hot' : '');
  byId('nodeAgent').className = 'node ' + (device.resolved_agent_id ? 'live ' : '');
  byId('nodeVoice').className = 'node ' + (device.latest_session?.has_audio_out ? 'live ' : '') + (device.latest_session?.last_error ? 'hot' : '');
  byId('nodeAI').className = 'node ' + (dashboard.services.find(s => s.name === 'AIGateway')?.ok ? 'live ' : '');
  byId('nodeMap').className = 'node ' + (device.mapping_count ? 'live ' : '');

  byId('nodeDeviceBody').innerHTML = `${esc(device.name)}<br><span class="muted">${esc(device.device_id)}</span><br><span class="muted">${device.online?'online':'offline'}, ${device.listening?'listening':'idle'}</span>`;
  byId('nodeAgentBody').innerHTML = `${esc(device.resolved_agent_name || 'Unassigned')}<br><span class="muted">${esc(device.resolved_agent_id || 'no agent selected')}</span>`;
  byId('nodeVoiceBody').innerHTML = device.latest_session?.last_error ? `<span class="error">${esc(device.latest_session.last_error)}</span>` : `${device.latest_session?.has_audio_out ? 'Audio output generated' : 'Awaiting audio turn'}`;
  byId('nodeAIBody').innerHTML = dashboard.services.find(s => s.name === 'AIGateway')?.ok ? 'LLM mapper reachable' : 'AIGateway unavailable';
  byId('nodeMapBody').innerHTML = `${device.mapping_count} saved mapping(s)<br><span class="muted">mode ${esc(device.preferred_render_mode || 'line')}</span>`;

  const signals = [];
  if(device.listening) signals.push('<span class="tag hot">listening</span>');
  if(device.latest_session?.timeline?.emotions?.length) device.latest_session.timeline.emotions.forEach(v => signals.push(`<span class="tag good">emotion ${esc(v)}</span>`));
  if(device.latest_session?.timeline?.actions?.length) device.latest_session.timeline.actions.forEach(v => signals.push(`<span class="tag hot">action ${esc(v)}</span>`));
  if(device.latest_session?.timeline?.visemes?.length) signals.push(`<span class="tag">visemes ${device.latest_session.timeline.visemes.length}</span>`);
  if(device.latest_session?.has_audio_in) signals.push('<span class="tag">audio in</span>');
  if(device.latest_session?.has_audio_out) signals.push('<span class="tag good">audio out</span>');
  byId('signalStrip').innerHTML = signals.join('') || '<span class="muted">No active signals.</span>';

  const eventStream = byId('eventStream');
  eventStream.innerHTML = (device.latest_session?.events || []).map(ev => `
    <div class="event">
      <strong>${esc(ev.event_type)}</strong>
      <div class="muted">${esc(fmtDate(ev.created_at))}</div>
      <pre>${esc(JSON.stringify(ev.payload, null, 2))}</pre>
    </div>
  `).join('') || '<div class="event muted">No session events yet.</div>';

  const sessionSelect = byId('sessionSelect');
  sessionSelect.innerHTML = device.sessions.map(s => `<option value="${esc(s.session_id)}" ${s.session_id===selectedSessionId?'selected':''}>${esc(short(s.session_id,8))} ${s.active?'(active)':'(closed)'}</option>`).join('');
  if(!selectedSessionId || !device.sessions.find(s => s.session_id === selectedSessionId)){
    selectedSessionId = device.sessions[0]?.session_id || '';
    sessionSelect.value = selectedSessionId;
  }
  if(device.latest_mapping){
    byId('mappingJson').value = JSON.stringify(device.latest_mapping, null, 2);
  } else {
    byId('mappingJson').value = '';
  }
  renderFiles(device);
}

function renderFiles(device){
  const session = device.sessions.find(s => s.session_id === selectedSessionId) || device.latest_session || null;
  byId('sessionSummary').textContent = session ? `${session.session_id} | ${session.active?'active':'closed'} | started ${fmtDate(session.started_at)}` : 'No session selected.';
  const box = byId('filesLinks');
  box.innerHTML = (session?.files || []).map(f => `<a href="${esc(f.download_url)}" target="_blank">${esc(f.path)} <span class="muted">(${f.size_bytes} bytes)</span></a>`).join('') || '<div class="event muted">No artifacts for this session yet.</div>';
}

async function saveBrain(){
  const device = dashboard.devices.find(d => d.device_id === selectedDeviceId);
  if(!device){ return; }
  const payload = {
    name: device.name,
    model: device.model,
    firmware_version: device.firmware_version,
    capabilities: {
      render_modes: device.render_modes,
      max_fps: device.max_fps,
      frame_budget_ms: device.frame_budget_ms,
      texture_kb: device.texture_kb,
      animations: device.animations,
      audio_codecs: device.audio_codecs,
      sample_rates: device.sample_rates,
      mic_enabled: device.mic_enabled,
      mic_format: device.mic_format,
      accepts_model_directives: device.accepts_model_directives,
      default_agent_id: byId('agentSelect').value
    }
  };
  await fetch(`/api/devices/${encodeURIComponent(device.device_id)}/capabilities`, {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify(payload)});
  await refreshAll(false);
}

async function loadMapping(){
  if(!selectedDeviceId || !byId('agentSelect').value){ return; }
  const res = await fetch(`/api/devices/${encodeURIComponent(selectedDeviceId)}/mappings/${encodeURIComponent(byId('agentSelect').value)}`);
  const data = await res.json();
  byId('modeSelect').value = data.preferred_render_mode || 'line';
  byId('mappingJson').value = JSON.stringify(data, null, 2);
}

async function suggestMapping(){
  if(!selectedDeviceId || !byId('agentSelect').value){ return; }
  const res = await fetch('/api/admin/mappings/suggest', {
    method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({device_id:selectedDeviceId, agent_id:byId('agentSelect').value, preferred_render_mode:byId('modeSelect').value})
  });
  const data = await res.json();
  byId('mappingJson').value = JSON.stringify(data, null, 2);
}

async function saveMapping(){
  if(!selectedDeviceId){ return; }
  const raw = JSON.parse(byId('mappingJson').value || '{}');
  await fetch(`/api/devices/${encodeURIComponent(selectedDeviceId)}/mappings`, {
    method:'PUT',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({
      agent_id: raw.agent_id || byId('agentSelect').value,
      preferred_render_mode: raw.preferred_render_mode || byId('modeSelect').value,
      emotion_map: raw.emotion_map || {},
      action_map: raw.action_map || {}
    })
  });
  await refreshAll(false);
}

async function loadSessionArtifacts(){
  selectedSessionId = byId('sessionSelect').value || '';
  await refreshAll(false);
}

async function disconnectSelected(){
  if(!selectedDeviceId){ return; }
  await fetch(`/api/admin/devices/${encodeURIComponent(selectedDeviceId)}/disconnect`, {method:'POST'});
  await refreshAll(false);
}

async function cleanupFakeDevices(){
  await fetch('/api/admin/devices/cleanup', {method:'POST'});
  selectedDeviceId = '';
  selectedSessionId = '';
  await refreshAll(false);
}

async function deleteSelectedDevice(){
  if(!selectedDeviceId){ return; }
  const device = dashboard.devices.find(d => d.device_id === selectedDeviceId);
  if(!device || device.online){
    alert('Only offline device records can be deleted.');
    return;
  }
  if(!confirm(`Delete stale device record ${selectedDeviceId}?`)){ return; }
  await fetch(`/api/admin/devices/${encodeURIComponent(selectedDeviceId)}/delete`, {method:'POST'});
  selectedDeviceId = '';
  selectedSessionId = '';
  await refreshAll(false);
}

async function pruneStaleDevices(){
  const hours = prompt('Delete offline devices unseen for at least how many hours?', '24');
  if(!hours){ return; }
  const value = Number(hours);
  if(!Number.isFinite(value) || value < 0){
    alert('Enter a valid number of hours.');
    return;
  }
  const res = await fetch(`/api/admin/devices/prune-stale?older_than_hours=${encodeURIComponent(String(value))}`, {method:'POST'});
  const data = await res.json();
  alert(`Removed ${data.removed.length} device record(s).`);
  selectedDeviceId = '';
  selectedSessionId = '';
  await refreshAll(false);
}

refreshAll(false);
setInterval(() => refreshAll(true).catch(()=>{}), 5000);
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


@router.get("/api/admin/dashboard")
async def dashboard(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    rows = await store.list_devices(db)
    agents = await get_agents()
    agent_names = {item.agent_id: item.name for item in agents}

    device_payloads: list[dict[str, Any]] = []
    for row in rows:
        if _is_fake_device(row.device_id, row.name, row.model):
            continue
        caps = store.device_capabilities_dict(row)
        telemetry = await _latest_telemetry(db, row.device_id)
        telemetry_payload = store.parse_event_payload(telemetry.raw_json) if telemetry is not None else {}
        listening = bool((telemetry_payload.get("extra") or {}).get("listening", False))
        resolved_agent_id, mapping_count = await _resolve_agent_id(db, row)
        sessions = await store.list_bridge_sessions(db, device_id=row.device_id, limit=8)
        session_payloads: list[dict[str, Any]] = []
        latest_session_payload: dict[str, Any] | None = None
        latest_mapping = None
        mapping_rows = await store.list_device_mappings(db, device_id=row.device_id)
        if mapping_rows:
            emotion_map, action_map = store.parse_mapping(mapping_rows[0])
            latest_mapping = {
                "agent_id": mapping_rows[0].agent_id,
                "device_id": mapping_rows[0].device_id,
                "preferred_render_mode": mapping_rows[0].preferred_render_mode,
                "emotion_map": {key: value.model_dump() for key, value in emotion_map.items()},
                "action_map": {key: value.model_dump() for key, value in action_map.items()},
            }
        for session in sessions:
            events = await store.list_session_events(db, session_id=session.session_id, limit=80)
            raw_event_payloads = [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                    "payload": store.parse_event_payload(event.payload_json),
                }
                for event in events
            ]
            event_payloads = [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                    "payload": _compact_payload(store.parse_event_payload(event.payload_json)),
                }
                for event in events
            ]
            timeline = _timeline_summary_from_events(raw_event_payloads)
            files = _session_files(session.session_id)
            payload = {
                "session_id": session.session_id,
                "agent_id": session.agent_id,
                "agent_name": agent_names.get(session.agent_id, session.agent_id),
                "upstream_session_id": session.upstream_session_id,
                "active": session.active,
                "started_at": session.started_at.isoformat() if session.started_at else None,
                "ended_at": session.ended_at.isoformat() if session.ended_at else None,
                "events": event_payloads,
                "timeline": timeline,
                "last_error": next((item["payload"].get("error", "") for item in reversed(event_payloads) if item["event_type"] == "ptt.error"), ""),
                "has_audio_in": any("/audio_in/" in file_info["path"] for file_info in files),
                "has_audio_out": any("/audio_out/" in file_info["path"] for file_info in files),
                "files": files,
            }
            session_payloads.append(payload)
            if latest_session_payload is None:
                latest_session_payload = payload

        device_payloads.append(
            {
                "device_id": row.device_id,
                "name": row.name,
                "model": row.model,
                "firmware_version": row.firmware_version,
                "online": hub.is_online(row.device_id),
                "listening": listening,
                "last_seen": telemetry.created_at.isoformat() if telemetry is not None and telemetry.created_at else None,
                "last_seen_human": telemetry.created_at.isoformat(timespec="seconds") if telemetry is not None and telemetry.created_at else "",
                "default_agent_id": str(caps.get("default_agent_id", "")).strip(),
                "resolved_agent_id": resolved_agent_id,
                "resolved_agent_name": agent_names.get(resolved_agent_id, resolved_agent_id),
                "mapping_count": mapping_count,
                "preferred_render_mode": latest_mapping["preferred_render_mode"] if latest_mapping else (caps.get("render_modes") or ["line"])[0],
                "render_modes": caps.get("render_modes", []),
                "max_fps": caps.get("max_fps", 30),
                "frame_budget_ms": caps.get("frame_budget_ms", 33),
                "texture_kb": caps.get("texture_kb", 512),
                "animations": caps.get("animations", []),
                "audio_codecs": caps.get("audio_codecs", []),
                "sample_rates": caps.get("sample_rates", []),
                "mic_enabled": caps.get("mic_enabled", True),
                "mic_format": caps.get("mic_format", "pcm16"),
                "accepts_model_directives": caps.get("accepts_model_directives", False),
                "latest_mapping": latest_mapping,
                "latest_session": latest_session_payload,
                "sessions": session_payloads,
            }
        )

    device_payloads.sort(key=lambda item: (not item["online"], item["name"].lower(), item["device_id"].lower()))

    services = [
        {"name": "DBS", "ok": True, "url": "/health", "status_code": 200},
        await _service_health("AgentManager", f"{settings.agentmanager_url.rstrip('/')}/health"),
        await _service_health("VoiceService", f"{settings.voiceservice_url.rstrip('/')}/health"),
        await _service_health("AIGateway", f"{settings.ai_gateway_url.rstrip('/')}/health"),
    ]
    return {"services": services, "devices": device_payloads, "agents": [item.model_dump() for item in agents]}


@router.post("/api/admin/devices/cleanup")
async def cleanup_fake_devices(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    rows = await store.list_devices(db)
    removed: list[str] = []
    for row in rows:
        if not _is_fake_device(row.device_id, row.name, row.model):
            continue
        await hub.force_disconnect(row.device_id)
        await store.delete_device(db, device_id=row.device_id)
        shutil.rmtree(DATA_ROOT / row.device_id, ignore_errors=True)
        removed.append(row.device_id)
    return {"status": "ok", "removed": removed}


@router.post("/api/admin/devices/{device_id}/delete")
async def delete_device_record(device_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await db.get(Device, device_id)
    if row is None:
        raise HTTPException(status_code=404, detail="device not found")
    if hub.is_online(device_id):
        raise HTTPException(status_code=409, detail="cannot delete online device")
    deleted = await store.delete_device(db, device_id=device_id)
    shutil.rmtree(DATA_ROOT / device_id, ignore_errors=True)
    return {"status": "ok", "deleted": deleted, "device_id": device_id}


@router.post("/api/admin/devices/prune-stale")
async def prune_stale_devices(older_than_hours: int = 24, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(hours=max(0, older_than_hours))
    rows = await store.list_devices(db)
    removed: list[str] = []
    for row in rows:
        if _is_fake_device(row.device_id, row.name, row.model):
            continue
        if hub.is_online(row.device_id):
            continue
        telemetry = await _latest_telemetry(db, row.device_id)
        updated_at = _as_utc(row.updated_at)
        last_seen = _as_utc(telemetry.created_at if telemetry is not None else None)
        if telemetry is None and updated_at is not None and updated_at >= cutoff:
            continue
        if last_seen is not None and last_seen >= cutoff:
            continue
        await store.delete_device(db, device_id=row.device_id)
        shutil.rmtree(DATA_ROOT / row.device_id, ignore_errors=True)
        removed.append(row.device_id)
    return {"status": "ok", "removed": removed, "older_than_hours": older_than_hours}


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
                "payload": _compact_payload(store.parse_event_payload(row.payload_json)),
            }
        )
    return out


@router.get("/api/admin/sessions/{session_id}/files")
async def list_session_files(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "files": _session_files(session_id)}


@router.get("/api/admin/files/download")
async def download_file(path: str) -> FileResponse:
    requested = Path(path).resolve()
    root = DATA_ROOT.resolve()
    if not requested.exists() or not requested.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if root not in requested.parents and requested != root:
        raise HTTPException(status_code=403, detail="path not allowed")
    return FileResponse(requested, filename=requested.name)


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
