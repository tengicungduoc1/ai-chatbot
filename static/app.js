'use strict';

if (document.getElementById('auth-form')) {
  const form = document.getElementById('auth-form');
  async function authenticate(action) {
    if (!form.reportValidity()) return;
    const fields = new FormData(form);
    const result = await fetch(`/api/auth/${action}`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'chatbot'},
      body: JSON.stringify({username: fields.get('username'), password: fields.get('password')})});
    const data = await result.json();
    if (!result.ok) throw new Error(data.error || 'Đăng nhập thất bại.');
    location.assign('/');
  }
  function submit(action) { authenticate(action).catch(error => { document.getElementById('auth-error').textContent = error.message; }); }
  form.addEventListener('submit', event => { event.preventDefault(); submit('login'); });
  document.getElementById('register').addEventListener('click', () => submit('register'));
} else {
const $ = id => document.getElementById(id);
const csrf = () => decodeURIComponent(document.cookie.split('; ').find(c => c.startsWith('chatbot_csrf='))?.split('=')[1] || '');
const VI_ZONE = 'Asia/Ho_Chi_Minh';
const state = {
  today: vnToday(), week: mondayOf(vnToday()), selectedDay: vnToday(), weekTouched: false,
  events: [], loadedWeek: '', calendarKey: '', statusKey: '', agendaKey: '', remindersKey: '', preferencesKey: '',
  initialized: false, refreshId: 0, editor: null, eventBusy: false,
  copySource: '', copyBusy: false, pending: null, chatBusy: false, preferencesBusy: false, preferencesDirty: false,
  assistantName: 'Trợ lý AI', assistantModel: '', aiAvailable: false, session: Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join(''), lastScrollWeek: '',

};

function vnToday(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: VI_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
  const part = type => parts.find(p => p.type === type).value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}
function addDays(iso, days) {
  const date = new Date(`${iso}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}
function mondayOf(iso) {
  const weekday = new Date(`${iso}T12:00:00Z`).getUTCDay();
  return addDays(iso, -(weekday === 0 ? 6 : weekday - 1));
}
function dateLabel(iso, options = {}) {
  return new Intl.DateTimeFormat('vi-VN', { day: '2-digit', month: '2-digit', ...options, timeZone: 'UTC' }).format(new Date(`${iso}T12:00:00Z`));
}
function weekLabel(week) {
  return `${dateLabel(week)} – ${dateLabel(addDays(week, 6), { year: 'numeric' })}`;
}
function minutes(time) {
  const [hour, minute] = time.split(':').map(Number);
  return hour * 60 + minute;
}
function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = String(text);
  return el;
}
function messageAt(id, text = '') {
  $(id).textContent = text;
  $(id).hidden = !text;
}
let toastTimer;
function notify(text) {
  clearTimeout(toastTimer);
  messageAt('toast', text);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6000);
}
async function api(path, method = 'GET', body) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method, credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(path === '/chat' ? 95000 : 20000),
      headers: method === 'GET' ? { Accept: 'application/json' } : {
        Accept: 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'chatbot', 'X-CSRF-Token': csrf(),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new Error(method === 'GET'
      ? 'Không kết nối được máy chủ. Dữ liệu đang hiển thị có thể chưa cập nhật.'
      : 'Mất kết nối khi gửi yêu cầu. Kiểm tra lại lịch trước khi thử lại; máy chủ có thể đã nhận thao tác.');
  }
  let data;
  try { data = response.status === 204 ? {} : await response.json(); }
  catch { throw new Error('Máy chủ trả về dữ liệu không hợp lệ. Hãy mở lại liên kết truy cập từ máy chủ.'); }
  if (response.status === 401 || response.status === 403) {
    throw new Error('Phiên đăng nhập đã hết hạn. Tải lại trang để đăng nhập lại.');
  }
  if (!response.ok) throw new Error(data.error || `Yêu cầu thất bại (HTTP ${response.status}).`);
  return data;
}

function updateWeekHeading() {
  $('week-label').textContent = weekLabel(state.week);
}
function updateClock() {
  $('clock-label').textContent = new Intl.DateTimeFormat('vi-VN', {
    timeZone: VI_ZONE, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(new Date());
}
function renderDevicePopover(devices) {
  const list = $('device-popover-list');
  list.replaceChildren();
  if (!devices.length) list.append(element('li', '', 'Chưa đăng ký thiết bị nào.'));
  for (const device of devices) {
    const item = element('li');
    item.append(element('strong', '', device.name || device.id), document.createTextNode(` · ${!device.online ? 'Ngoại tuyến' : device.busy ? 'Đang bận' : 'Sẵn sàng'}`));
    list.append(item);
  }
} 
function renderConflictWarning() {
  const date = $('event-date').value, start = $('event-start').value, end = $('event-end').value;
  if (!date || !start || !end || end <= start) { messageAt('event-conflict'); return; }
  const conflict = state.events.find(item => item.date === date && item.id !== state.editor?.id && start < item.end && item.start < end);
  messageAt('event-conflict', conflict ? `Trùng với “${conflict.title}” (${conflict.start}-${conflict.end}). Lịch không bị ghi đè; máy chủ sẽ xác nhận xung đột.` : '');
}
function renderCalendar() {
  const key = JSON.stringify([state.week, state.today, state.events]);
  if (key === state.calendarKey) return;
  state.calendarKey = key;
  const grid = $('week-grid');
  const focusedId = document.activeElement?.dataset.eventId;
  const sameWeek = state.lastScrollWeek === state.week;
  const scrollTop = $('calendar-scroll').scrollTop;
  const scrollLeft = $('calendar-scroll').scrollLeft;
  const fragment = document.createDocumentFragment();
  fragment.append(element('div', 'time-header', 'GMT+7'));
  const days = ['Thứ Hai', 'Thứ Ba', 'Thứ Tư', 'Thứ Năm', 'Thứ Sáu', 'Thứ Bảy', 'Chủ Nhật'];
  for (let i = 0; i < 7; i++) {
    const date = addDays(state.week, i);
    const header = element('div', `day-header${date === state.today ? ' today' : ''}`);
    header.append(element('span', '', days[i]), element('strong', '', Number(date.slice(8))));
    if (date === state.today) header.setAttribute('aria-label', `${days[i]}, ${dateLabel(date)}, hôm nay`);
    fragment.append(header);
  }
  const axis = element('div', 'time-axis');
  axis.setAttribute('aria-hidden', 'true');
  const periods = { 0: 'Cả ngày', 6: 'Sáng', 12: 'Chiều', 18: 'Tối' };
  for (let hour = 0; hour < 24; hour++) {
    const label = element('div', `hour-label${periods[hour] ? ' period-label' : ''}`);
    label.append(element('time', '', `${String(hour).padStart(2, '0')}:00`));
    if (periods[hour]) label.append(element('span', 'period-name', periods[hour]));
    axis.append(label);
  }
  fragment.append(axis);
  for (let i = 0; i < 7; i++) {
    const date = addDays(state.week, i);
    const column = element('div', `day-column${date === state.today ? ' today' : ''}${i > 4 ? ' weekend' : ''}`);
    column.style.height = '1728px';
    column.setAttribute('role', 'group');
    column.setAttribute('aria-label', `${days[i]}, ${dateLabel(date)}`);
    for (const hour of [6, 12, 18]) {
      const divider = element('span', 'period-divider');
      divider.setAttribute('aria-hidden', 'true');
      divider.style.top = `${hour * 72}px`;
      column.append(divider);
    }
    column.addEventListener('pointermove', event => {
      if (event.target.closest('button')) return;
      const rect = column.getBoundingClientRect();
      column.style.setProperty('--hover-time', `${Math.max(0, Math.min(99, event.clientY - rect.top))}px`);
    });
    for (const event of state.events.filter(e => e.date === date)) {
      const duration = minutes(event.end) - minutes(event.start);
      const button = element('button', `calendar-event${duration < 35 ? ' short-event' : ''}`);
      button.type = 'button';
      button.dataset.eventId = event.id;
      button.style.top = `${minutes(event.start) * 1.2}px`;
      button.style.height = `${Math.max(18, duration * 1.2 - 3)}px`;
      button.setAttribute('aria-label', `${event.title}, ${dateLabel(event.date)}, ${event.start} đến ${event.end}. Chỉnh sửa lịch.`);
      button.title = `${event.title}\n${event.start} – ${event.end}${event.note ? `\n${event.note}` : ''}`;
      button.append(element('time', '', `${event.start} – ${event.end}`), element('strong', '', event.title));
      if (event.note && duration >= 75) button.append(element('span', 'event-note', event.note));
      if (duration >= 100) button.append(element('span', 'small', event.remind_minutes === 0 ? 'Nhắc đúng giờ' : `Nhắc trước ${event.remind_minutes} phút`));
      button.addEventListener('click', () => openEvent(event));
      column.append(button);
    }
    column.addEventListener('click', event => {
      if (event.target.closest('button')) return;
      const rect = column.getBoundingClientRect();
      const clickedMinutes = Math.max(0, Math.min(1439, Math.floor((event.clientY - rect.top) / 1.2)));
      const startMinutes = Math.min(23 * 60 + 45, Math.round(clickedMinutes / 15) * 15);
      const endMinutes = Math.min(23 * 60 + 59, startMinutes + 60);
      const format = value => `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
      state.selectedDay = date;
      renderDayNavigation();
      renderDayAgenda();
      openEvent(null, { date, start: format(startMinutes), end: format(endMinutes) });
    });
    fragment.append(column);
  }
  grid.replaceChildren(fragment);
  $('calendar-empty').hidden = state.events.length !== 0;
  $('calendar-summary').textContent = `${state.events.length} lịch trong tuần · 24 giờ mỗi ngày · Không tự động lặp lại`;
  const firstEventMinute = state.events.length ? Math.min(...state.events.map(e => minutes(e.start))) : 7 * 60;
  $('calendar-scroll').scrollTop = sameWeek ? scrollTop : Math.max(6, Math.floor(firstEventMinute / 60) - 1) * 72;
  $('calendar-scroll').scrollLeft = sameWeek ? scrollLeft : 0;
  state.lastScrollWeek = state.week;
  renderDayNavigation();
  renderDayAgenda();
  if (focusedId) {
    [...grid.querySelectorAll('[data-event-id]')].find(el => el.dataset.eventId === focusedId)?.focus({ preventScroll: true });
  }
}
function dayName(iso, options = { weekday: 'long', day: 'numeric', month: 'long' }) {
  return new Intl.DateTimeFormat('vi-VN', { ...options, timeZone: 'UTC' }).format(new Date(`${iso}T12:00:00Z`));
}
function renderDayNavigation() {
  const nav = $('day-navigation');
  nav.replaceChildren();
  const weekdays = ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN'];
  for (let i = 0; i < 7; i++) {
    const date = addDays(state.week, i);
    const button = element('button', `day-choice${date === state.selectedDay ? ' selected' : ''}${date === state.today ? ' today' : ''}`);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(date === state.selectedDay));
    button.setAttribute('aria-label', `${dayName(date)}, ${dateLabel(date)}${date === state.today ? ', hôm nay' : ''}`);
    button.append(element('span', '', weekdays[i]), element('strong', '', Number(date.slice(8))));
    if (date === state.today) button.append(element('small', '', 'Hôm nay'));
    button.addEventListener('click', () => selectDay(date));
    nav.append(button);
  }
}
function renderDayAgenda(error = '', loading = false) {
  const list = $('day-agenda');
  list.replaceChildren();
  list.setAttribute('aria-busy', String(loading));
  if (loading) {
    list.append(element('p', 'state-message', 'Đang tải lịch trong ngày…'));
    return;
  }
  $('selected-day-label').textContent = dayName(state.selectedDay);
  renderDayNavigation();
  if (error) {
    list.append(element('p', 'state-message error-state', error));
    const retry = element('button', 'text-button', 'Thử tải lại');
    retry.type = 'button';
    retry.addEventListener('click', () => { void refreshAll(); });
    list.append(retry);
    return;
  }
  const events = state.events.filter(event => event.date === state.selectedDay).sort((a, b) => a.start.localeCompare(b.start));
  if (!events.length) {
    const empty = element('div', 'day-empty');
    empty.append(element('strong', '', 'Ngày này chưa có lịch.'), element('p', '', 'Khoảng trống để học, nghỉ ngơi hoặc lên kế hoạch tiếp theo.'));
    const add = element('button', 'button subtle', '+ Thêm lịch cho ngày này');
    add.type = 'button';
    add.addEventListener('click', () => { openEvent(); $('event-date').value = state.selectedDay; });
    empty.append(add);
    list.append(empty);
    return;
  }
  for (const event of events) {
    const button = element('button', 'day-event');
    button.type = 'button';
    button.dataset.eventId = event.id;
    button.setAttribute('aria-label', `${event.title}, ${event.start} đến ${event.end}. Chỉnh sửa lịch.`);
    const time = element('time', 'day-event-time', `${event.start} - ${event.end}`);
    time.dateTime = `${event.date}T${event.start}:00+07:00`;
    button.append(time, element('strong', '', event.title));
    if (event.note) button.append(element('span', 'day-event-note', event.note));
    button.addEventListener('click', () => openEvent(event));
    list.append(button);
  }
}
function selectDay(date) {
  let changedWeek = false;
  if (date < state.week || date > addDays(state.week, 6)) {
    state.weekTouched = true;
    state.week = mondayOf(date);
    state.calendarKey = '';
    $('calendar-summary').textContent = 'Đang tải lịch tuần…';
    changedWeek = true;
  }
  state.selectedDay = date;
  if (changedWeek || state.loadedWeek !== state.week) renderDayAgenda('', true);
  else renderDayAgenda();
  if (changedWeek || state.loadedWeek !== state.week) void refreshAll();
}
function renderPreferences(data) {
  const models = Array.isArray(data.models) ? data.models.filter(value => typeof value === 'string' && value.trim()) : [];
  const voices = Array.isArray(data.voices) ? data.voices.filter(value => typeof value === 'string' && value.trim()) : [];
  const key = JSON.stringify([data.assistant_name, data.style_prompt, data.model, data.voice, models, voices]);
  if (key === state.preferencesKey) return;
  if (state.preferencesDirty || state.preferencesBusy) return;
  state.preferencesKey = key;
  const options = (select, values, selected) => {
    select.replaceChildren();
    for (const value of values) {
      const option = element('option', '', value);
      option.value = value;
      option.selected = value === selected;
      select.append(option);
    }
  };
  $('assistant-name').value = typeof data.assistant_name === 'string' ? data.assistant_name : '';
  $('style-prompt').value = typeof data.style_prompt === 'string' ? data.style_prompt : '';
  options($('preference-model'), models, data.model);
  options($('preference-voice'), voices, data.voice);
  state.assistantName = data.assistant_name || 'Trợ lý AI';
  state.assistantModel = data.model || '';
  $('model-label').textContent = state.assistantName;
  messageAt('preference-error');
  $('preference-status').textContent = 'Tùy chỉnh đã tải.';
}
$('preferences-form').addEventListener('input', () => { state.preferencesDirty = true; });
$('preferences-form').addEventListener('change', () => { state.preferencesDirty = true; });
$('preferences-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.preferencesBusy) return;
  const body = {
    assistant_name: $('assistant-name').value.trim(),
    style_prompt: $('style-prompt').value.trim(),
    model: $('preference-model').value,
    voice: $('preference-voice').value,
  };
  if (!body.assistant_name) {
    messageAt('preference-error', 'Hãy nhập tên trợ lý.');
    $('assistant-name').focus();
    return;
  }
  if (!body.model || !body.voice) {
    messageAt('preference-error', 'Hãy chọn mô hình và giọng đọc.');
    return;
  }
  state.preferencesBusy = true;
  $('preferences-form').querySelectorAll('input,textarea,select,button').forEach(control => { control.disabled = true; });
  $('preference-status').textContent = 'Đang lưu tùy chỉnh…';
  messageAt('preference-error');
  try {
    const result = await api('/preferences', 'PUT', body);
    state.preferencesDirty = false;
    state.preferencesBusy = false;
    state.preferencesKey = '';
    renderPreferences(result);
    $('preference-status').textContent = 'Tùy chỉnh đã lưu.';
    notify('Đã lưu tùy chỉnh trợ lý.');
  } catch (error) {
    $('preference-status').textContent = 'Chưa lưu được tùy chỉnh.';
    messageAt('preference-error', error.message);
  } finally {
    state.preferencesBusy = false;
    $('preferences-form').querySelectorAll('input,textarea,select,button').forEach(control => { control.disabled = false; });
  }
});
function renderAgenda(data) {
  const key = JSON.stringify(data);
  if (key === state.agendaKey) return;
  state.agendaKey = key;
  for (const [id, events, title, text] of [
    ['today-list', data.today, 'Hôm nay chưa có lịch.', 'Một khoảng trống để dành cho điều quan trọng.'],
    ['upcoming-list', data.upcoming, 'Chưa có lịch sắp tới.', 'Lịch mới của bạn sẽ xuất hiện ở đây.'],
  ]) {
    const container = $(id);
    const focusedId = container.contains(document.activeElement) ? document.activeElement.dataset.eventId : null;
    container.replaceChildren();
    if (!events.length) {
      const empty = element('p', 'agenda-empty');
      empty.append(element('strong', '', title), document.createTextNode(text));
      container.append(empty);
    }
    for (const event of events) {
      const button = element('button', 'agenda-event');
      button.type = 'button';
      button.dataset.eventId = event.id;
      button.append(element('time', '', `${id === 'upcoming-list' ? `${dateLabel(event.date)} · ` : ''}${event.start} – ${event.end}`), element('strong', '', event.title));
      if (event.note) button.append(element('small', '', event.note));
      button.addEventListener('click', () => openEvent(event));
      container.append(button);
    }
    if (focusedId) [...container.children].find(el => el.dataset.eventId === focusedId)?.focus({ preventScroll: true });
  }
}
function renderStatus(data) {
  const previousToday = state.today || vnToday(new Date(data.now));
  state.aiAvailable = Boolean(data.ai_configured);
  $('ai-status').textContent = data.ai_configured ? 'AI đã cấu hình' : 'AI chưa cấu hình';
  $('ai-status').className = `status-pill ${data.ai_configured ? 'online' : 'offline'}`;
  $('model-label').textContent = state.assistantName;
  $('ai-help').hidden = data.ai_configured;
  const audioNote = document.querySelector('.reminders-panel .panel-note');
  audioNote.textContent = data.last_audio_mode === 'beep'
    ? 'Âm thanh dự phòng: chuông và chữ trên màn hình, chưa có giọng đọc. “Đã phát” là thiết bị xác nhận phát xong.'
    : 'Chỉ hiển thị “Đã phát” khi thiết bị xác nhận phát xong.';
  const online = data.devices.filter(device => device.online);
  $('device-status').textContent = online.length ? `${online.length} thiết bị kết nối` : 'Thiết bị ngoại tuyến';
  $('device-status').className = `status-pill ${online.length ? 'online' : 'offline'}`;
  const key = JSON.stringify(data.devices);
  if (key !== state.statusKey) {
    state.statusKey = key;
    $('device-list').replaceChildren();
    if (!data.devices.length) $('device-list').append(element('li', '', 'Chưa đăng ký thiết bị. Nhấn Đăng ký thiết bị để ghép nối.'));
    for (const device of data.devices) {
      const item = element('li');
      item.append(element('strong', '', device.name || device.id), document.createTextNode(` · ${!device.online ? 'Ngoại tuyến' : device.busy ? 'Đang bận' : 'Sẵn sàng'}`));
      $('device-list').append(item);
    }
    renderDevicePopover(data.devices);
  }
  const date = vnToday(new Date(data.now));
  if (date !== previousToday) {
    const stillOnPreviousToday = state.week === mondayOf(previousToday) && state.selectedDay === previousToday;
    state.today = date;
    $('today-label').textContent = dateLabel(date, { weekday: 'long', year: 'numeric' });
    if (!state.weekTouched && stillOnPreviousToday) {
      state.week = mondayOf(date);
      state.selectedDay = date;
      state.calendarKey = '';
      state.loadedWeek = '';
    }
  } else {
    $('today-label').textContent = dateLabel(date, { weekday: 'long', year: 'numeric' });
  }
  if ($('device-popover').hidden === false) $('popover-sync').textContent = $('sync-status').textContent;
  setChatControls();
}
function renderReminders(data) {
  const key = JSON.stringify(data.reminders);
  if (key === state.remindersKey) return;
  state.remindersKey = key;
  $('reminder-list').replaceChildren();
  if (!data.reminders.length) $('reminder-list').append(element('p', '', 'Chưa có lịch sử nhắc. Đặt thời gian nhắc khi thêm hoặc chỉnh sửa lịch.'));
  const labels = { played: 'Đã phát', failed: 'Phát thất bại', cancelled: 'Đã hủy phát', pending: 'Đang chờ phát', sent: 'Đã gửi, chờ xác nhận', delivered: 'Đã gửi, chờ xác nhận', expired: 'Đã hết hạn' };
  for (const reminder of data.reminders) {
    const item = element('div', 'reminder-item');
    item.append(element('strong', '', reminder.event?.title || reminder.title || reminder.text || 'Nhắc lịch'), element('span', '', labels[reminder.status] || 'Chưa xác nhận đã phát'));
    $('reminder-list').append(item);
  }
}

let refreshInFlight = 0;
async function refreshAll(background = false) {
  if (background && refreshInFlight) return false;
  refreshInFlight++;
  try { return await refreshSnapshot(); }
  finally { refreshInFlight--; }
}
async function refreshSnapshot() {
  const requestId = ++state.refreshId;
  const isFirstRefresh = !state.initialized;
  const errors = [];
  try {
    const status = await api('/status');
    if (requestId !== state.refreshId) return false;
    renderStatus(status);
    if (!state.initialized) {
      if (!state.weekTouched) state.week = mondayOf(state.today);
      state.selectedDay = state.today;
    }
    state.initialized = true;
  } catch (error) {
    errors.push(error.message);
    if (requestId === state.refreshId) {
      state.aiAvailable = false;
      $('ai-status').textContent = 'AI · mất kết nối';
      $('device-status').textContent = 'Thiết bị · chưa rõ';
      $('ai-status').className = $('device-status').className = 'status-pill offline';
      setChatControls();
    }
  }
  if (requestId !== state.refreshId) return false;
  const wasInitialized = state.initialized;
  if (!wasInitialized) state.selectedDay = state.today;
  const requestedWeek = state.week;
  updateWeekHeading();
  if (isFirstRefresh) renderDayAgenda('', true);
  const results = await Promise.allSettled([api(`/events?week=${encodeURIComponent(requestedWeek)}`), api('/agenda'), api('/reminders'), api('/preferences')]);
  if (requestId !== state.refreshId || requestedWeek !== state.week) return false;
  const handlers = [data => { state.events = data.events; state.loadedWeek = requestedWeek; renderCalendar(); }, renderAgenda, renderReminders, renderPreferences];
  results.forEach((result, index) => {
    if (result.status === 'fulfilled') handlers[index](result.value);
    else {
      errors.push(result.reason.message);
      if (index === 0) renderDayAgenda(result.reason.message);
      if (index === 3) {
        state.preferencesKey = '';
        $('preference-status').textContent = 'Không tải được tùy chỉnh.';
        messageAt('preference-error', result.reason.message);
      }
    }
  });
  if (results[0].status === 'rejected') renderDayAgenda(results[0].reason.message);
  messageAt('connection-error', [...new Set(errors)].join(' '));
  $('sync-status').textContent = errors.length ? 'Chưa đồng bộ đầy đủ' : `Đã đồng bộ ${new Intl.DateTimeFormat('vi-VN', { timeZone: VI_ZONE, hour: '2-digit', minute: '2-digit' }).format(new Date())}`;
  return errors.length === 0;
}
async function changeWeek(week) {
  state.weekTouched = true;
  state.week = week;
  if (state.selectedDay < week || state.selectedDay > addDays(week, 6)) {
    state.selectedDay = state.today >= week && state.today <= addDays(week, 6) ? state.today : week;
  }
  state.calendarKey = '';
  updateWeekHeading();
  // Clear only the calendar on navigation; drafts and chat DOM are never rebuilt by polling.
  $('week-grid').replaceChildren();
  $('calendar-empty').hidden = true;
  $('calendar-summary').textContent = 'Đang tải lịch tuần…';
  await refreshAll();
}

function openEvent(event = null, defaults = null) {
  state.editor = event ? { ...event } : null;
  $('event-form').reset();
  messageAt('event-error');
  $('delete-confirmation').hidden = true;
  $('delete-event').hidden = !event;
  $('event-dialog-title').textContent = event ? 'Chỉnh sửa lịch' : 'Thêm lịch mới';
  $('event-title').value = event?.title || '';
  $('event-date').value = event?.date || defaults?.date || state.selectedDay;
  $('event-start').value = event?.start || defaults?.start || '08:00';
  $('event-end').value = event?.end || defaults?.end || '09:00';
  $('event-note').value = event?.note || '';
  $('save-event').disabled = false;
  $('event-reminder').value = event?.remind_minutes ?? 10;
  $('event-end').setCustomValidity('');
  renderConflictWarning();
  $('event-dialog').showModal();
  $('event-title').focus();
}
for (const id of ['event-title', 'event-date', 'event-start', 'event-end']) {
  $(id).addEventListener('input', renderConflictWarning);
  $(id).addEventListener('change', renderConflictWarning);
}
function eventLock(busy) {
  state.eventBusy = busy;
  $('event-form').querySelectorAll('input,textarea,button').forEach(control => { control.disabled = busy; });
  $('save-event').textContent = busy ? 'Đang lưu…' : 'Lưu lịch';
}
$('event-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.eventBusy) return;
  const payload = {
    title: $('event-title').value.trim(), date: $('event-date').value,
    start: $('event-start').value, end: $('event-end').value,
    note: $('event-note').value, remind_minutes: Number($('event-reminder').value),
  };
  if (!payload.title) { messageAt('event-error', 'Hãy nhập tên lịch, không chỉ khoảng trắng.'); $('event-title').focus(); return; }
  if (payload.end <= payload.start) { messageAt('event-error', 'Giờ kết thúc phải sau giờ bắt đầu trong cùng ngày.'); $('event-end').focus(); return; }
  messageAt('event-error');
  eventLock(true);
  try {
    const current = state.editor;
    await api(current ? `/events/${encodeURIComponent(current.id)}` : '/events', current ? 'PUT' : 'POST', current ? { ...payload, revision: current.revision } : payload);
    state.selectedDay = payload.date;
    if (payload.date < state.week || payload.date > addDays(state.week, 6)) {
      state.week = mondayOf(payload.date);
      state.weekTouched = true;
      state.calendarKey = '';
    }
    $('event-dialog').close();
    const refreshed = await refreshAll();
    notify(refreshed ? 'Đã lưu lịch của bạn.' : 'Máy chủ đã nhận lịch. Chưa tải lại được dữ liệu mới.');
  } catch (error) { messageAt('event-error', error.message); }
  finally { eventLock(false); }
});
$('delete-event').addEventListener('click', () => {
  $('delete-summary').textContent = `${state.editor.title} · ${dateLabel(state.editor.date)} · ${state.editor.start} – ${state.editor.end}`;
  $('delete-confirmation').hidden = false;
  $('save-event').disabled = true;
  $('cancel-delete').focus();
});
$('cancel-delete').addEventListener('click', () => {
  $('delete-confirmation').hidden = true;
  $('save-event').disabled = false;
  $('delete-event').focus();
});
$('confirm-delete').addEventListener('click', async () => {
  if (state.eventBusy || !state.editor) return;
  messageAt('event-error');
  eventLock(true);
  try {
    await api(`/events/${encodeURIComponent(state.editor.id)}`, 'DELETE', { revision: state.editor.revision });
    $('event-dialog').close();
    const refreshed = await refreshAll();
    notify(refreshed ? 'Đã xóa lịch.' : 'Máy chủ đã nhận yêu cầu xóa. Chưa tải lại được dữ liệu mới.');
  } catch (error) { messageAt('event-error', error.message); }
  finally { eventLock(false); $('save-event').disabled = !$('delete-confirmation').hidden; }
});
$('event-dialog').addEventListener('cancel', event => { if (state.eventBusy) event.preventDefault(); });

function updateCopyTarget() {
  const date = $('copy-target').value;
  $('copy-target-hint').textContent = date ? `Tuần đích: ${weekLabel(mondayOf(date))} (bắt đầu thứ Hai).` : '';
}
$('copy-week').addEventListener('click', () => {
  state.copySource = state.week;
  $('copy-source').textContent = `Tuần nguồn: ${weekLabel(state.copySource)}`;
  $('copy-target').value = addDays(state.copySource, 7);
  updateCopyTarget();
  messageAt('copy-error');
  $('copy-dialog').showModal();
  $('copy-target').focus();
});
$('copy-target').addEventListener('input', updateCopyTarget);
$('copy-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.copyBusy) return;
  const target = mondayOf($('copy-target').value);
  if (target === state.copySource) { messageAt('copy-error', 'Hãy chọn một tuần khác tuần nguồn.'); return; }
  messageAt('copy-error');
  state.copyBusy = true;
  $('copy-form').querySelectorAll('input,button').forEach(el => { el.disabled = true; });
  $('copy-submit').textContent = 'Đang sao chép…';
  try {
    await api('/weeks/copy', 'POST', { source_week: state.copySource, target_week: target });
    $('copy-dialog').close();
    await changeWeek(target);
    notify('Đã sao chép lịch. Đang xem tuần đích.');
  } catch (error) { messageAt('copy-error', error.message); }
  finally {
    state.copyBusy = false;
    $('copy-form').querySelectorAll('input,button').forEach(el => { el.disabled = false; });
    $('copy-submit').textContent = 'Sao chép tuần';
  }
});
$('copy-dialog').addEventListener('cancel', event => { if (state.copyBusy) event.preventDefault(); });

function appendMessage(role, text) {
  const log = $('chat-log');
  const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  const top = log.scrollTop;
  $('chat-welcome')?.remove();
  const item = element('div', `message ${role}`);
  item.append(element('span', 'message-label', role === 'user' ? 'Bạn' : 'Trợ lý AI'), element('div', 'message-body', text));
  log.append(item);
  log.scrollTop = follow ? log.scrollHeight : top;
}
function setChatControls() {
  $('chat-send').disabled = state.chatBusy || !state.aiAvailable || Boolean(state.pending);
  $('chat-confirm').disabled = state.chatBusy || !state.aiAvailable;
  $('chat-cancel').disabled = state.chatBusy || !state.aiAvailable;
  $('ask-today') && ($('ask-today').disabled = state.chatBusy || !state.aiAvailable || Boolean(state.pending));
  $('chat-input').placeholder = state.pending ? 'Xác nhận hoặc hủy đề xuất trước khi gửi tin tiếp theo…' : 'Bạn muốn sắp xếp điều gì?';
  $('chat-send').textContent = state.chatBusy ? 'Đang gửi…' : 'Gửi ↗';
  $('chat-state').textContent = state.chatBusy ? 'Trợ lý đang xử lý…' : state.pending ? 'Đang chờ bạn xác nhận. Chưa có thay đổi nào được thực hiện.' : 'Lịch chỉ thay đổi sau khi bạn xác nhận.';
}
function renderPending() {
  $('chat-confirmation').hidden = !state.pending;
  $('chat-proposal').textContent = state.pending?.summary || '';
  setChatControls();
}
async function sendChat(message, confirmation) {
  if (state.chatBusy || !state.aiAvailable) return;
  state.chatBusy = true;
  messageAt('chat-error');
  setChatControls();
  appendMessage('user', message);
  try {
    const result = await api('/chat', 'POST', {
      session_id: state.session, message, week: state.week,
      ...(confirmation ? { confirmation } : {}),
    });
    appendMessage('assistant', result.reply);
    state.pending = result.pending || null;
    renderPending();
    if (result.changed) await refreshAll();
  } catch (error) {
    messageAt('chat-error', error.message);
    if (!confirmation && !state.pending && !$('chat-input').value) $('chat-input').value = message;
  } finally { state.chatBusy = false; setChatControls(); }
}
$('chat-form').addEventListener('submit', event => {
  event.preventDefault();
  const message = $('chat-input').value.trim();
  if (!message || state.pending || state.chatBusy || !state.aiAvailable) return;
  $('chat-input').value = '';
  void sendChat(message);
});
$('chat-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('chat-form').requestSubmit();
  }
});
$('ask-today').addEventListener('click', () => {
  $('chat-input').value = 'Hôm nay tôi có lịch gì?';
  $('chat-form').requestSubmit();
});
$('chat-confirm').addEventListener('click', () => { if (state.pending) void sendChat('Xác nhận', state.pending.token); });
$('chat-cancel').addEventListener('click', () => { if (state.pending) void sendChat('Hủy'); });
$('add-event').addEventListener('click', () => openEvent());
$('add-first-event').addEventListener('click', () => openEvent());
for (const hour of [0, 6, 12, 18]) {
  $(`period-jump-${String(hour).padStart(2, '0')}`).addEventListener('click', () => {
    $('calendar-scroll').scrollTo({ top: hour * 72, behavior: 'smooth' });
  });
}
$('previous-week').addEventListener('click', () => { void changeWeek(addDays(state.week, -7)); });
$('next-week').addEventListener('click', () => { void changeWeek(addDays(state.week, 7)); });
$('current-week').addEventListener('click', () => { state.selectedDay = state.today; void changeWeek(mondayOf(state.today)); });
$('previous-day').addEventListener('click', () => selectDay(addDays(state.selectedDay, -1)));
$('next-day').addEventListener('click', () => selectDay(addDays(state.selectedDay, 1)));
$('refresh').addEventListener('click', () => { void refreshAll(); });
function closeDevicePopover(returnFocus = false) {
  $('device-popover').hidden = true;
  $('device-status').setAttribute('aria-expanded', 'false');
  if (returnFocus) $('device-status').focus();
}
$('device-status').addEventListener('click', () => {
  const open = $('device-popover').hidden;
  $('device-popover').hidden = !open;
  $('device-status').setAttribute('aria-expanded', String(open));
  if (open) { closeAccountMenu(); $('popover-sync').textContent = $('sync-status').textContent; }
});
$('popover-refresh').addEventListener('click', () => { closeDevicePopover(); void refreshAll(); });
function openPreferences() { closeAccountMenu(); $('preferences-dialog').showModal(); $('assistant-name').focus(); }
$('open-settings').addEventListener('click', openPreferences);
$('account-settings').addEventListener('click', openPreferences);
$('preferences-close').addEventListener('click', () => $('preferences-dialog').close());
$('preferences-dialog').addEventListener('click', event => { if (event.target === $('preferences-dialog')) $('preferences-dialog').close(); });
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (!$('device-popover').hidden) closeDevicePopover(true);
  if (!$('account-popover').hidden) closeAccountMenu(true);
  if ($('preferences-dialog').open) {
    event.preventDefault();
    $('preferences-dialog').close();
    $('open-settings').focus();
  }
});
document.addEventListener('click', event => {
  if (!$('device-popover').hidden && !event.target.closest('.device-menu')) closeDevicePopover();
});
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => {
  $(button.dataset.close).close();
  if (button.dataset.close === 'event-dialog') $('save-event').disabled = false;
}));

updateWeekHeading();
updateClock();
setInterval(updateClock, 15000);
setChatControls();
void refreshAll();
// ponytail: light single-user polling; use server push if many clients need live updates.
setInterval(() => { if (!document.hidden) void refreshAll(true); }, 10000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) void refreshAll(true); });

function closeAccountMenu(returnFocus = false) {
  $('account-popover').hidden = true;
  $('account-trigger').setAttribute('aria-expanded', 'false');
  if (returnFocus) $('account-trigger').focus();
}
$('account-trigger').addEventListener('click', () => {
  const open = $('account-popover').hidden;
  closeDevicePopover();
  $('account-popover').hidden = !open;
  $('account-trigger').setAttribute('aria-expanded', String(open));
});
document.addEventListener('click', event => {
  if (!event.target.closest('#account-menu')) closeAccountMenu();
});
$('logout').addEventListener('click', async () => {
  $('logout').disabled = true;
  try { await api('/auth/logout', 'POST', {}); location.assign('/'); }
  catch (error) { notify(error.message); $('logout').disabled = false; }
});
function openDeviceRegistration() {
  closeDevicePopover();
  $('device-register-form').reset();
  $('device-register-form').hidden = false;
  $('device-result').hidden = true;
  $('device-token').textContent = '';
  messageAt('device-error');
  $('device-register-dialog').showModal();
  $('device-register-id').focus();
}
$('add-device').addEventListener('click', openDeviceRegistration);
$('popover-add-device').addEventListener('click', openDeviceRegistration);
$('device-register-close').addEventListener('click', () => $('device-register-dialog').close());
$('device-register-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('device-register-submit');
  button.disabled = true;
  messageAt('device-error');
  try {
    const result = await api('/devices', 'POST', {device_id: form.elements.device_id.value.trim(), name: form.elements.name.value.trim()});
    $('device-token').textContent = result.token;
    form.hidden = true;
    $('device-result').hidden = false;
    $('device-copy-status').textContent = '';
    void refreshAll(true);
  } catch (error) { messageAt('device-error', error.message); }
  finally { button.disabled = false; }
});
$('device-copy-token').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('device-token').textContent);
    $('device-copy-status').textContent = 'Đã sao chép mã kết nối.';
  } catch { $('device-copy-status').textContent = 'Không sao chép được. Hãy chọn mã và sao chép thủ công.'; }
});
$('device-register-dialog').addEventListener('close', () => { $('device-token').textContent = ''; $('device-result').hidden = true; });
void api('/auth/me').then(data => { $('account-identity').textContent = data.user?.username || 'Đã đăng nhập'; }).catch(() => {});
}
