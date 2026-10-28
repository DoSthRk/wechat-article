// 时间输入始终按北京时间解释，不使用操作者电脑的时区。
export function beijingInput(milliseconds = Date.now()) {
  return new Date(milliseconds + 8 * 60 * 60 * 1000).toISOString().slice(0, 16);
}

export function scheduledISO(value, milliseconds = Date.now()) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw Error('请选择定时发布时间（北京时间）');
  const date = new Date(`${value}:00+08:00`);
  if (!Number.isFinite(date.getTime()) || beijingInput(date.getTime()) !== value) throw Error('请选择有效日期和时间');
  if (date.getTime() - milliseconds < 60000) throw Error('发布时间需至少晚于当前时间 1 分钟');
  return date.toISOString();
}

export function beijingTime(value) {
  return new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}) + '（北京时间）';
}
