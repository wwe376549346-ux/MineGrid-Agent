import React, {useEffect, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Drawer, DrawerHeader} from "@astryxdesign/core/Drawer";
import {Spinner} from "@astryxdesign/core/Spinner";
import {Plus, Trash2} from "lucide-react";
import {apiRequest} from "./api.js";

const CATEGORIES = {
  semantic: "语义红线",
  episodic: "排障案例",
  procedural: "操作规程",
};
const NEW_MEMORY = {category: "semantic", title: "", content: "", scope_cabinet: "", tags: "", voltageLimit: ""};
const jsonHeaders = {"Content-Type": "application/json"};

export function MemoryPlaybookDrawer({draft, onClose}) {
  const [category, setCategory] = useState(draft?.category ?? "semantic");
  const [form, setForm] = useState(draft ? {...NEW_MEMORY, ...draft} : null);
  const [items, setItems] = useState([]);
  const [deleteId, setDeleteId] = useState(null);
  const [editing, setEditing] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const request = new AbortController();
    setLoading(true);
    setError("");
    apiRequest("/api/memory?category=" + encodeURIComponent(category), {signal: request.signal})
      .then(payload => { if (!request.signal.aborted) setItems(payload.memories); })
      .catch(err => { if (!request.signal.aborted) setError(err.message); })
      .finally(() => { if (!request.signal.aborted) setLoading(false); });
    return () => request.abort();
  }, [category, refresh]);

  function selectCategory(next) {
    setItems([]);
    setCategory(next);
    setDeleteId(null);
    setEditing(null);
    setNotice("");
  }

  async function mutate(path, options, success, onSuccess, onFailure) {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await apiRequest(path, options);
      onSuccess?.();
      setNotice(success);
      setRefresh(value => value + 1);
    } catch (err) {
      onFailure?.();
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  function saveMemory(event) {
    event.preventDefault();
    const {scope_cabinet, tags, voltageLimit, ...draftFields} = form;
    const memory = {
      ...draftFields,
      scope_cabinet: scope_cabinet.trim() || null,
      tags: [...new Set(tags.split(/[,，]/).map(tag => tag.trim()).filter(Boolean))],
    };
    if (form.category === "semantic" && voltageLimit) {
      memory.metric_threshold = {metric: "Uab", operator: ">", value: Number(voltageLimit), unit: "kV"};
    }
    mutate("/api/memory", {method: "POST", headers: jsonHeaders, body: JSON.stringify(memory)}, "已写入长期记忆", () => {
      if (category !== memory.category) selectCategory(memory.category);
      setForm(null);
    });
  }

  function itemPath(id) {
    return "/api/memory/" + encodeURIComponent(id);
  }

  function patchMemory(id, changes, success, onSuccess, onFailure) {
    mutate(itemPath(id), {method: "PATCH", headers: jsonHeaders, body: JSON.stringify(changes)}, success, onSuccess, onFailure);
  }

  function toggleMemory(id, enabled) {
    const previous = items;
    setItems(current => current.map(item => (item.memory_id ?? item.id) === id ? {...item, enabled} : item));
    patchMemory(id, {enabled}, enabled ? "记忆已启用" : "记忆已停用", undefined, () => setItems(previous));
  }

  const field = (key, value) => setForm(current => ({...current, [key]: value}));
  const close = open => { if (!open) onClose(); };
  return <Drawer isOpen onOpenChange={close} width="580px" label="长期记忆管理"
    experimental={{isFullWidthOnMobile: true}}>
    <DrawerHeader title="长期记忆管理" onOpenChange={close} hasDivider
      endContent={<Button label="新增记忆" icon={<Plus size={16} />} size="sm" variant="primary" disabled={saving}
        onClick={() => setForm({...NEW_MEMORY, category})} />} />
    <style>{`
      .memory-playbook { display: grid; gap: 14px; padding: 12px 0 22px; color: var(--duty-ink, var(--text)); }
      .memory-playbook h3, .memory-playbook p { margin: 0; }
      .memory-playbook-tabs, .memory-playbook-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
      .memory-playbook-tabs button { font: inherit; font-size: 13px; padding: 7px 10px; border: 1px solid var(--border);
        border-radius: 8px; background: var(--surface); color: inherit; cursor: pointer; }
      .memory-playbook-tabs button[aria-pressed="true"] { border-color: var(--duty-accent, var(--accent)); background: var(--duty-accent-soft, var(--surface-subtle)); font-weight: 650; }
      .memory-playbook-card, .memory-playbook-form { display: grid; gap: 9px; min-width: 0; padding: 13px;
        border: 1px solid var(--border); border-radius: 12px; background: var(--surface); overflow-wrap: anywhere; }
      .memory-playbook-card > header { display: flex; gap: 7px; justify-content: space-between; align-items: baseline; }
      .memory-playbook-card h3 { font-size: 14px; }
      .memory-playbook-card p { white-space: pre-wrap; font-size: 13px; line-height: 1.55; }
      .memory-playbook-card label, .memory-playbook-form label { display: grid; gap: 4px; font-size: 13px; font-weight: 550; min-width: 0; }
      .memory-playbook-field { box-sizing: border-box; width: 100%; min-width: 0; padding: 8px 9px; border: 1px solid var(--border-strong);
        border-radius: 8px; background: var(--surface-subtle); color: inherit; font: inherit; }
      textarea.memory-playbook-field { resize: vertical; }
      .memory-playbook-pair { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 8px; }
      .memory-playbook-actions label { display: inline-flex; align-items: center; gap: 5px; margin-right: auto; }
      .memory-playbook-actions input { accent-color: var(--duty-accent, var(--accent)); }
      .memory-playbook-meta { font-size: 12px; color: var(--duty-muted, var(--muted)); }
      .memory-playbook-alert { color: var(--duty-alert, var(--danger)); }
    `}</style>
    <div className="memory-playbook">
      <div className="memory-playbook-tabs" role="group" aria-label="记忆类别">
        {Object.entries(CATEGORIES).map(([id, label]) => <button key={id} type="button" aria-pressed={id === category}
          disabled={saving} onClick={() => { if (id !== category) selectCategory(id); }}>{label}</button>)}
      </div>

      {form && <form className="memory-playbook-form" onSubmit={saveMemory}>
        <div className="memory-playbook-pair">
          <label>类别<select className="memory-playbook-field" value={form.category} onChange={e => field("category", e.target.value)}>
            {Object.entries(CATEGORIES).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select></label>
          <label>适用柜号<input className="memory-playbook-field" value={form.scope_cabinet} maxLength={24}
            onChange={e => field("scope_cabinet", e.target.value)} placeholder="可选" /></label>
        </div>
        <label>标题<input className="memory-playbook-field" value={form.title} maxLength={140}
          onChange={e => field("title", e.target.value)} required /></label>
        <label>记忆内容<textarea aria-label="记忆内容" className="memory-playbook-field" value={form.content} maxLength={2000} rows={5}
          onChange={e => field("content", e.target.value)} required /></label>
        {form.category === "semantic" && <label>线电压上限（kV）
          <input className="memory-playbook-field" type="number" min="0.01" step="any" value={form.voltageLimit}
            onChange={e => field("voltageLimit", e.target.value)} placeholder="可选，超过此数值时提示" />
        </label>}
        <label>标签<input className="memory-playbook-field" value={form.tags}
          onChange={e => field("tags", e.target.value)} placeholder="可选，以逗号分隔" /></label>
        <div className="memory-playbook-actions">
          <Button type="submit" label={saving ? "保存中…" : "保存记忆"} variant="primary" size="sm"
            disabled={saving || !form.title.trim() || !form.content.trim()} />
          <Button type="button" label="取消" size="sm" disabled={saving} onClick={() => setForm(null)} />
        </div>
      </form>}

      {error && <div className="memory-playbook-alert" role="alert">
        <p>{error}</p>
        {!form && !saving && <Button label="重新读取" size="sm" onClick={() => setRefresh(value => value + 1)} />}
      </div>}
      {notice && <p className="memory-playbook-meta" role="status">{notice}</p>}
      {loading ? <div role="status"><Spinner size="sm" /> 正在读取长期记忆</div>
        : !error && items.length === 0 ? <p className="memory-playbook-meta">暂无{CATEGORIES[category]}。</p> : null}
      {!loading && items.map(item => {
        const id = item.memory_id ?? item.id;
        const isEditing = editing?.id === id;
        return <article key={id} className="memory-playbook-card">
          <header><h3>{item.title}</h3><Badge variant={item.enabled ? "success" : "neutral"} label={item.enabled ? "已启用" : "已停用"} /></header>
          {isEditing ? <form className="memory-playbook-form" onSubmit={e => {
            e.preventDefault();
            patchMemory(id, {content: editing.content}, "记忆内容已更新", () => setEditing(null));
          }}>
            <label>修改记忆内容<textarea aria-label="修改记忆内容" rows={4} className="memory-playbook-field" maxLength={2000}
              value={editing.content} onChange={e => setEditing({id, content: e.target.value})} required /></label>
            <div className="memory-playbook-actions">
              <Button type="submit" size="sm" label="保存修改" disabled={saving || !editing.content.trim()} />
              <Button type="button" size="sm" label="取消修改" disabled={saving} onClick={() => setEditing(null)} />
            </div>
          </form> : <p>{item.content}</p>}
          {(item.scope_cabinet || item.tags?.length > 0 || item.metric_threshold != null) && <div className="memory-playbook-meta">
            {[item.scope_cabinet && `${item.scope_cabinet} 柜`,
              item.metric_threshold != null && `阈值 ${typeof item.metric_threshold === "object"
                ? [item.metric_threshold.metric, item.metric_threshold.operator, item.metric_threshold.value, item.metric_threshold.unit].filter(v => v != null).join(" ")
                : item.metric_threshold}`,
              ...(item.tags ?? [])].filter(Boolean).join(" · ")}
          </div>}
          <div className="memory-playbook-actions">
            <label><input type="checkbox" role="switch" aria-label={`启用 ${item.title}`} checked={Boolean(item.enabled)}
              disabled={saving} onChange={e => toggleMemory(id, e.target.checked)} />启用</label>
            {deleteId === id ? <>
              <Button size="sm" label="确认删除" disabled={saving}
                onClick={() => mutate(itemPath(id), {method: "DELETE"}, "记忆已删除", () => setDeleteId(null))} />
              <Button size="sm" label="取消删除" disabled={saving} onClick={() => setDeleteId(null)} />
            </> : <>
              {!isEditing && <Button size="sm" label="编辑内容" disabled={saving}
                onClick={() => setEditing({id, content: item.content})} />}
              <Button size="sm" label="删除" icon={<Trash2 size={14} />} disabled={saving} onClick={() => setDeleteId(id)} />
            </>}
          </div>
        </article>;
      })}
    </div>
  </Drawer>;
}
