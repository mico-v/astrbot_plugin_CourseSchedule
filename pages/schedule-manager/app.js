const bridge = window.AstrBotPluginPage;

const state = {
  scopes: [],
  selectedScopeId: "",
  selectedUserId: "",
  schedule: null,
  dirty: false,
};

const addMembers = {
  scopeId: "",
  label: "",
  members: [],
  selected: new Set(),
  filter: "",
  loading: false,
  error: "",
};

const transfer = {
  scopeId: "",
  label: "",
  memberCount: 0,
  file: null,
};

const $ = (selector) => document.querySelector(selector);
const notice = $("#notice");
const scopeList = $("#scopeList");
const courseList = $("#courseList");
const courseTemplate = $("#courseTemplate");

function showNotice(message, type = "") {
  notice.textContent = message || "";
  notice.className = `notice${message ? " show" : ""}${type ? ` ${type}` : ""}`;
}

function setDirty(value) {
  state.dirty = value;
  $("#dirtyMark").classList.toggle("hidden", !value);
}

function canLeaveEditor() {
  return !state.dirty || window.confirm("当前课表有未保存修改，确定要放弃吗？");
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  if (busy) button.dataset.oldText = button.textContent;
  button.textContent = busy ? "处理中…" : button.dataset.oldText || button.textContent;
}

function scopeMatches(scope, query) {
  if (!query) return true;
  const haystack = [
    scope.label,
    scope.scope_id,
    scope.target_id,
    ...(scope.members || []).flatMap((member) => [member.name, member.user_id]),
  ]
    .join(" ")
    .toLocaleLowerCase();
  return haystack.includes(query.toLocaleLowerCase());
}

function makeMemberButton(scope, member) {
  const wrapper = document.createElement("div");
  const button = document.createElement("button");
  button.type = "button";
  button.className = `member-item${
    state.selectedScopeId === scope.scope_id && state.selectedUserId === member.user_id
      ? " active"
      : ""
  }`;
  button.addEventListener("click", () => selectMember(scope.scope_id, member.user_id));

  const title = document.createElement("div");
  title.className = "member-item-title";
  title.textContent = member.name || member.user_id;
  const meta = document.createElement("div");
  meta.className = "member-item-meta";
  meta.textContent = `${member.user_id} · ${member.event_count || 0} 节课`;
  button.append(title, meta);
  wrapper.append(button);
  return wrapper;
}

function renderScopes() {
  const query = $("#scopeSearch").value.trim();
  scopeList.replaceChildren();
  const visible = state.scopes.filter((scope) => scopeMatches(scope, query));
  $("#scopeCount").textContent = String(visible.length);

  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = state.scopes.length ? "没有匹配的群组或成员。" : "还没有保存任何群组课表。";
    scopeList.append(empty);
    return;
  }

  for (const scope of visible) {
    const section = document.createElement("section");
    const scopeButton = document.createElement("button");
    scopeButton.type = "button";
    scopeButton.className = `scope-item${state.selectedScopeId === scope.scope_id ? " active" : ""}`;
    scopeButton.addEventListener("click", () => {
      if (state.selectedScopeId === scope.scope_id) return;
      if (state.dirty && !canLeaveEditor()) return;
      state.selectedScopeId = scope.scope_id;
      const first = scope.members?.[0];
      state.selectedUserId = first?.user_id || "";
      state.schedule = null;
      setDirty(false);
      renderScopes();
      if (first) loadMember(scope.scope_id, first.user_id);
    });

    const row = document.createElement("div");
    row.className = "scope-row";
    row.append(scopeButton);

    const transferButton = document.createElement("button");
    transferButton.type = "button";
    transferButton.className = "scope-add";
    transferButton.title = `导入 / 导出「${scope.label}」`;
    transferButton.setAttribute("aria-label", `导入或导出${scope.label}`);
    transferButton.textContent = "⇅";
    transferButton.addEventListener("click", (event) => {
      event.stopPropagation();
      openTransfer(scope);
    });
    row.append(transferButton);

    if (scope.kind === "group") {
      const addButton = document.createElement("button");
      addButton.type = "button";
      addButton.className = "scope-add";
      addButton.title = `为「${scope.label}」添加成员课表`;
      addButton.setAttribute("aria-label", `为${scope.label}添加成员课表`);
      addButton.textContent = "＋";
      addButton.addEventListener("click", (event) => {
        event.stopPropagation();
        openAddMembers(scope);
      });
      row.append(addButton);
    }

    const title = document.createElement("div");
    title.className = "scope-item-title";
    title.textContent = scope.label;
    const meta = document.createElement("div");
    meta.className = "scope-item-meta";
    meta.textContent = `${scope.member_count || 0} 位成员 · ${scope.event_count || 0} 节课`;
    scopeButton.append(title, meta);
    section.append(row);

    if (state.selectedScopeId === scope.scope_id) {
      const members = document.createElement("div");
      members.className = "member-list";
      for (const member of scope.members || []) members.append(makeMemberButton(scope, member));
      section.append(members);
    }
    scopeList.append(section);
  }
}

function setEditorVisible(visible) {
  $("#emptyState").classList.toggle("hidden", visible);
  $("#editor").classList.toggle("hidden", !visible);
}

function currentScope() {
  return state.scopes.find((scope) => scope.scope_id === state.selectedScopeId);
}

function selectMember(scopeId, userId) {
  if (state.selectedScopeId === scopeId && state.selectedUserId === userId && state.schedule) {
    return;
  }
  if (state.dirty && !canLeaveEditor()) {
    return;
  }
  state.selectedScopeId = scopeId;
  state.selectedUserId = userId;
  state.schedule = null;
  setDirty(false);
  renderScopes();
  loadMember(scopeId, userId);
}

function renderEditor() {
  if (!state.schedule) {
    setEditorVisible(false);
    return;
  }
  setEditorVisible(true);
  const scope = currentScope();
  $("#scopeLabel").textContent = scope?.label || state.schedule.scope_id;
  $("#memberTitle").textContent = `${state.schedule.name || state.schedule.user_id} 的课表`;
  $("#memberMeta").textContent = `当前版本 ${state.schedule.revision} · 修改后保存会同步更新本地 ICS`;
  $("#memberName").value = state.schedule.name || "";
  $("#memberId").textContent = state.schedule.user_id;
  courseList.replaceChildren();
  for (const event of state.schedule.events || []) addCourseCard(event);
  updateCourseIndexes();
  $("#noCourses").classList.toggle("hidden", courseList.children.length > 0);
}

function addCourseCard(event = {}) {
  const card = courseTemplate.content.firstElementChild.cloneNode(true);
  card.dataset.uid = event.uid || "";
  card.dataset.eventId = event.id || "";
  for (const field of card.querySelectorAll("[data-field]")) {
    field.value = event[field.dataset.field] || "";
    field.addEventListener("input", () => setDirty(true));
    field.addEventListener("change", () => setDirty(true));
  }
  card.querySelector(".remove-course").addEventListener("click", () => {
    card.remove();
    updateCourseIndexes();
    $("#noCourses").classList.toggle("hidden", courseList.children.length > 0);
    setDirty(true);
  });
  courseList.append(card);
  $("#noCourses").classList.add("hidden");
  updateCourseIndexes();
}

function updateCourseIndexes() {
  [...courseList.children].forEach((card, index) => {
    card.querySelector(".course-index").textContent = String(index + 1).padStart(2, "0");
  });
}

function collectSchedule() {
  const events = [...courseList.children].map((card) => {
    const event = {};
    for (const field of card.querySelectorAll("[data-field]")) event[field.dataset.field] = field.value.trim();
    if (card.dataset.uid) event.uid = card.dataset.uid;
    if (card.dataset.eventId) event.id = card.dataset.eventId;
    return event;
  });
  return {
    scope_id: state.schedule.scope_id,
    user_id: state.schedule.user_id,
    revision: state.schedule.revision,
    name: $("#memberName").value.trim(),
    events,
  };
}

function validateSchedule(payload) {
  if (!payload.name) return "成员名称不能为空。";
  for (const [index, event] of payload.events.entries()) {
    if (!event.course) return `第 ${index + 1} 节课程缺少课程名称。`;
    if (!event.start || !event.end) return `第 ${index + 1} 节课程需要填写完整的上课和下课时间。`;
    if (event.end <= event.start) return `第 ${index + 1} 节课程的下课时间必须晚于上课时间。`;
  }
  return "";
}

async function loadMember(scopeId, userId) {
  state.selectedScopeId = scopeId;
  state.selectedUserId = userId;
  renderScopes();
  showNotice("正在读取成员课表…");
  try {
    state.schedule = await bridge.apiGet("schedule", { scope_id: scopeId, user_id: userId });
    setDirty(false);
    renderEditor();
    showNotice("");
    return true;
  } catch (error) {
    state.schedule = null;
    renderEditor();
    showNotice(error.message || "读取课表失败。", "error");
    return false;
  }
}

async function loadScopes({ keepSelection = true } = {}) {
  const previousScope = keepSelection ? state.selectedScopeId : "";
  const previousUser = keepSelection ? state.selectedUserId : "";
  showNotice("正在读取群组和成员…");
  try {
    const data = await bridge.apiGet("scopes");
    state.scopes = Array.isArray(data?.scopes) ? data.scopes : [];
    const scope = state.scopes.find((item) => item.scope_id === previousScope) || state.scopes[0];
    state.selectedScopeId = scope?.scope_id || "";
    const member = scope?.members?.find((item) => item.user_id === previousUser) || scope?.members?.[0];
    state.selectedUserId = member?.user_id || "";
    renderScopes();
    if (scope && member) await loadMember(scope.scope_id, member.user_id);
    else {
      state.schedule = null;
      renderEditor();
      showNotice(state.scopes.length ? "请选择一个成员开始编辑。" : "当前还没有可编辑的课表。", state.scopes.length ? "" : "error");
    }
  } catch (error) {
    showNotice(error.message || "读取群组列表失败。", "error");
  }
}

async function saveSchedule() {
  if (!state.schedule) return;
  const payload = collectSchedule();
  const validationError = validateSchedule(payload);
  if (validationError) {
    showNotice(validationError, "error");
    return;
  }
  const button = $("#saveButton");
  setBusy(button, true);
  showNotice("正在保存课表…");
  try {
    const saved = await bridge.apiPost("schedule/save", payload);
    state.schedule.revision = saved.revision;
    state.schedule.name = payload.name;
    state.schedule.events = payload.events;
    setDirty(false);
    renderScopes();
    renderEditor();
    showNotice(`保存成功，共 ${saved.event_count} 节课程。`);
  } catch (error) {
    showNotice(error.message || "保存失败，请刷新后重试。", "error");
  } finally {
    setBusy(button, false);
  }
}

function roleLabel(role) {
  return { owner: "群主", admin: "管理员" }[role] || "";
}

function visiblePicks() {
  const query = addMembers.filter.trim().toLocaleLowerCase();
  if (!query) return addMembers.members;
  return addMembers.members.filter((member) =>
    `${member.name || ""} ${member.user_id}`.toLocaleLowerCase().includes(query),
  );
}

function makePickRow(member) {
  const row = document.createElement("label");
  row.className = `pick-row${addMembers.selected.has(member.user_id) ? " checked" : ""}`;

  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = addMembers.selected.has(member.user_id);
  box.addEventListener("change", () => {
    if (box.checked) addMembers.selected.add(member.user_id);
    else addMembers.selected.delete(member.user_id);
    row.classList.toggle("checked", box.checked);
    renderPickerMeta();
  });

  const text = document.createElement("div");
  text.className = "pick-text";
  const name = document.createElement("span");
  name.className = "pick-name";
  name.textContent = member.name || member.user_id;
  const meta = document.createElement("span");
  meta.className = "pick-meta";
  const role = roleLabel(member.role);
  meta.textContent = `${member.user_id}${role ? ` · ${role}` : ""}`;
  text.append(name, meta);

  row.append(box, text);
  return row;
}

function renderPickerMeta() {
  $("#addMemberCount").textContent = String(addMembers.selected.size);
  const submit = $("#addMemberSubmit");
  submit.disabled = addMembers.loading || addMembers.selected.size === 0;
  submit.textContent = addMembers.selected.size
    ? `添加课表（${addMembers.selected.size}）`
    : "添加课表";
}

function renderPicker() {
  const list = $("#addMemberList");
  const empty = $("#addMemberEmpty");
  const visible = visiblePicks();
  list.replaceChildren();
  for (const member of visible) list.append(makePickRow(member));

  const place = addMembers.label || addMembers.scopeId;
  let emptyText = "";
  if (addMembers.loading) {
    $("#addMemberMeta").textContent = `${place} · 正在读取群成员…`;
    emptyText = "正在读取群成员…";
  } else if (addMembers.error) {
    $("#addMemberMeta").textContent = `${place} · 读取失败`;
    emptyText = addMembers.error;
  } else {
    $("#addMemberMeta").textContent = `${place} · 可添加 ${addMembers.members.length} 位成员`;
    if (!addMembers.members.length) emptyText = "该群的成员都已经有课表了。";
    else if (!visible.length) emptyText = "没有匹配的成员。";
  }
  empty.textContent = emptyText;
  empty.classList.toggle("hidden", !emptyText);
  renderPickerMeta();
}

function openAddMembers(scope) {
  addMembers.scopeId = scope.scope_id;
  addMembers.label = scope.label;
  addMembers.filter = "";
  addMembers.members = [];
  addMembers.selected = new Set();
  addMembers.error = "";
  $("#addMemberSearch").value = "";
  $("#addMemberDialog").classList.remove("hidden");
  loadAddMembers();
}

function closeAddMembers() {
  $("#addMemberDialog").classList.add("hidden");
  addMembers.scopeId = "";
  addMembers.label = "";
  addMembers.members = [];
  addMembers.selected = new Set();
  addMembers.error = "";
  addMembers.loading = false;
}

async function loadAddMembers() {
  addMembers.loading = true;
  addMembers.error = "";
  addMembers.members = [];
  addMembers.selected = new Set();
  renderPicker();
  try {
    const data = await bridge.apiGet("members", { scope_id: addMembers.scopeId });
    addMembers.members = Array.isArray(data?.members) ? data.members : [];
  } catch (error) {
    addMembers.error = error.message || "读取群成员失败，请确认机器人已连接。";
  } finally {
    addMembers.loading = false;
  }
  renderPicker();
}

function toggleAllVisible() {
  const visible = visiblePicks();
  const allSelected = visible.length > 0 && visible.every((member) => addMembers.selected.has(member.user_id));
  for (const member of visible) {
    if (allSelected) addMembers.selected.delete(member.user_id);
    else addMembers.selected.add(member.user_id);
  }
  renderPicker();
}

async function submitAddMembers() {
  const chosen = addMembers.members.filter((member) => addMembers.selected.has(member.user_id));
  if (!chosen.length) return;
  const scopeId = addMembers.scopeId;
  const button = $("#addMemberSubmit");
  setBusy(button, true);
  try {
    const result = await bridge.apiPost("schedule/create", {
      scope_id: scopeId,
      members: chosen.map((member) => ({ user_id: member.user_id, name: member.name })),
    });
    closeAddMembers();
    const created = Array.isArray(result?.created) ? result.created : chosen;
    await loadScopes();
    let selected = true;
    if (created.length) selected = await loadMember(scopeId, created[0].user_id);
    if (selected) {
      showNotice(`已为 ${created.length} 位成员创建课表，可以开始编辑课程。`);
    }
  } catch (error) {
    showNotice(error.message || "添加课表失败。", "error");
  } finally {
    setBusy(button, false);
  }
}

async function refresh() {
  if (!canLeaveEditor()) return;
  const button = $("#refreshButton");
  setBusy(button, true);
  try {
    await loadScopes();
  } finally {
    setBusy(button, false);
  }
}

/* ---------------------------------------------------------------- 导入导出 */

function timestamp() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return [
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`,
    `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`,
  ].join("-");
}

function safeFileLabel(label) {
  return String(label || "会话").replace(/[\\/:*?"<>|]+/g, "-").trim() || "会话";
}

function formatSize(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function canExport() {
  return (
    !state.dirty ||
    window.confirm("当前课表有未保存的修改，导出的内容是已保存的版本。确定继续吗？")
  );
}

async function downloadExport(endpoint, params, filename) {
  showNotice(`正在导出 ${filename}…`);
  try {
    await bridge.download(endpoint, params, filename);
    showNotice(`已开始下载 ${filename}。`);
    return true;
  } catch (error) {
    showNotice(error.message || "导出失败，请刷新页面后重试。", "error");
    return false;
  }
}

async function exportMemberIcs() {
  if (!state.schedule || !canExport()) return;
  const { scope_id: scopeId, user_id: userId } = state.schedule;
  await downloadExport(
    "schedule/export",
    { scope_id: scopeId, user_id: userId },
    `schedule${userId}.ics`,
  );
}

async function exportScopeArchive(format) {
  if (!transfer.scopeId || !canExport()) return;
  const scope = state.scopes.find((item) => item.scope_id === transfer.scopeId);
  const label = safeFileLabel(scope?.label || transfer.label);
  const suffix = format === "backup" ? "原始备份" : "ICS";
  await downloadExport(
    "schedule/export",
    { scope_id: transfer.scopeId, format },
    `课表-${suffix}-${label}-${timestamp()}.zip`,
  );
}

function importSummary(result) {
  const summary = result || {};
  const parts = [
    `已导入 ${summary.member_count || 0} 位成员的课表（新增 ${summary.created_count || 0}、` +
      `覆盖 ${summary.updated_count || 0}），共 ${summary.event_count || 0} 节课程。`,
  ];
  if (summary.day_override_count) {
    parts.push(`同时恢复 ${summary.day_override_count} 条休假/调休标记。`);
  }
  const skipped = Array.isArray(summary.skipped) ? summary.skipped : [];
  if (skipped.length) {
    const shown = skipped.slice(0, 3).join("、");
    parts.push(`忽略 ${skipped.length} 个文件：${shown}${skipped.length > 3 ? "…" : ""}`);
  }
  return parts.join("");
}

function selectedMemberName(scopeId) {
  if (state.selectedScopeId !== scopeId || !state.selectedUserId) return "";
  const scope = state.scopes.find((item) => item.scope_id === scopeId);
  const member = scope?.members?.find((item) => item.user_id === state.selectedUserId);
  return member ? `${member.name || member.user_id}（${member.user_id}）` : "";
}

function renderTransfer() {
  $("#transferMeta").textContent = `${transfer.label} · ${transfer.memberCount} 位成员`;
  $("#transferScopeCount").textContent = String(transfer.memberCount);
  $("#transferExportIcs").disabled = transfer.memberCount === 0;
  $("#transferExportBackup").disabled = transfer.memberCount === 0;
  $("#transferFileName").textContent = transfer.file
    ? `${transfer.file.name} · ${formatSize(transfer.file.size)}`
    : "未选择 .zip 或 .ics 文件";
  $("#transferImport").disabled = !transfer.file;

  const member = selectedMemberName(transfer.scopeId);
  $("#transferHint").textContent = member
    ? `单个 .ics 会导入到当前选中的 ${member}；文件名为 schedule<QQ号>.ics 时以文件名为准。`
    : "本会话还没有选中成员：导入单个 .ics 前请先选中成员，或把文件命名为 schedule<QQ号>.ics。";
}

function openTransfer(scope) {
  transfer.scopeId = scope.scope_id;
  transfer.label = scope.label;
  transfer.memberCount = scope.member_count || 0;
  transfer.file = null;
  $("#transferFile").value = "";
  $("#transferDialog").classList.remove("hidden");
  renderTransfer();
}

function closeTransfer() {
  $("#transferDialog").classList.add("hidden");
  transfer.scopeId = "";
  transfer.label = "";
  transfer.memberCount = 0;
  transfer.file = null;
  $("#transferFile").value = "";
}

async function importArchive() {
  const file = transfer.file;
  if (!file || !transfer.scopeId) return;
  if (!canLeaveEditor()) return;
  const scopeId = transfer.scopeId;
  const button = $("#transferImport");
  setBusy(button, true);
  showNotice("正在导入…");
  try {
    // A .zip carries its own member list, so it goes up as a file; a single
    // .ics is text and names its target in the body.
    const result = file.name.toLocaleLowerCase().endsWith(".ics")
      ? await bridge.apiPost("schedule/import", {
          scope_id: scopeId,
          user_id: selectedMemberName(scopeId) ? state.selectedUserId : "",
          filename: file.name,
          content: await file.text(),
        })
      : await bridge.upload(`import/${scopeId}`, file);
    closeTransfer();
    await loadScopes();
    showNotice(importSummary(result));
  } catch (error) {
    showNotice(error.message || "导入失败，请检查文件后重试。", "error");
  } finally {
    setBusy(button, false);
  }
}

async function start() {
  const context = await bridge.ready();
  document.title = bridge.t("pages.schedule-manager.title", context?.pageTitle || "课表管理");
  bridge.onContext((nextContext) => {
    document.documentElement.dataset.theme = nextContext?.isDark ? "dark" : "light";
  });
  $("#refreshButton").addEventListener("click", refresh);
  $("#scopeSearch").addEventListener("input", renderScopes);
  $("#addCourseButton").addEventListener("click", () => {
    addCourseCard();
    setDirty(true);
  });
  $("#memberName").addEventListener("input", () => {
    $("#memberTitle").textContent = `${$("#memberName").value || state.schedule?.user_id || "成员"} 的课表`;
    setDirty(true);
  });
  $("#saveButton").addEventListener("click", saveSchedule);
  $("#addMemberClose").addEventListener("click", closeAddMembers);
  $("#addMemberCancel").addEventListener("click", closeAddMembers);
  $("#addMemberReload").addEventListener("click", loadAddMembers);
  $("#addMemberSubmit").addEventListener("click", submitAddMembers);
  $("#addMemberSelectAll").addEventListener("click", toggleAllVisible);
  $("#addMemberSearch").addEventListener("input", (event) => {
    addMembers.filter = event.target.value;
    renderPicker();
  });
  $("#addMemberDialog").addEventListener("click", (event) => {
    if (event.target === $("#addMemberDialog")) closeAddMembers();
  });
  $("#exportMemberButton").addEventListener("click", exportMemberIcs);
  $("#transferClose").addEventListener("click", closeTransfer);
  $("#transferExportIcs").addEventListener("click", () => exportScopeArchive("ics"));
  $("#transferExportBackup").addEventListener("click", () => exportScopeArchive("backup"));
  $("#transferImport").addEventListener("click", importArchive);
  $("#transferFile").addEventListener("change", (event) => {
    transfer.file = event.target.files?.[0] || null;
    renderTransfer();
  });
  $("#transferDialog").addEventListener("click", (event) => {
    if (event.target === $("#transferDialog")) closeTransfer();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("#addMemberDialog").classList.contains("hidden")) closeAddMembers();
    else if (!$("#transferDialog").classList.contains("hidden")) closeTransfer();
  });
  await loadScopes({ keepSelection: false });
}

start().catch((error) => showNotice(error.message || "页面初始化失败。", "error"));
