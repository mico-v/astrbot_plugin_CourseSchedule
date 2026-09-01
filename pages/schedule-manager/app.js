const bridge = window.AstrBotPluginPage;

const state = {
  scopes: [],
  selectedScopeId: "",
  selectedUserId: "",
  schedule: null,
  dirty: false,
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

    const title = document.createElement("div");
    title.className = "scope-item-title";
    title.textContent = scope.label;
    const meta = document.createElement("div");
    meta.className = "scope-item-meta";
    meta.textContent = `${scope.member_count || 0} 位成员 · ${scope.event_count || 0} 节课`;
    scopeButton.append(title, meta);
    section.append(scopeButton);

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
  } catch (error) {
    state.schedule = null;
    renderEditor();
    showNotice(error.message || "读取课表失败。", "error");
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
  await loadScopes({ keepSelection: false });
}

start().catch((error) => showNotice(error.message || "页面初始化失败。", "error"));
