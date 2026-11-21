export const numFmt = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 3});
export const formatNum = value => (value == null ? "—" : numFmt.format(value));
export const signedKw = value => (value > 0 ? "+" : value < 0 ? "−" : "") + formatNum(Math.abs(value));

export const sourceUrl = file => "/api/source/" + encodeURIComponent(file);

export function cellHref(cellRef) {
  if (!cellRef || typeof cellRef !== "string") return null;
  const match = cellRef.match(/(\d{4}\.xls)/);
  return match ? sourceUrl(match[1]) : null;
}

export const AUTH_TOKEN_KEY = "laowadi_auth_token";

export function getAuthToken() {
  try {
    return window.localStorage?.getItem(AUTH_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setAuthToken(token) {
  try {
    if (token) window.localStorage?.setItem(AUTH_TOKEN_KEY, token);
    else window.localStorage?.removeItem(AUTH_TOKEN_KEY);
  } catch {
    // ignore storage quota errors
  }
}

export function authHeaders(extra = {}) {
  const token = getAuthToken();
  const headers = {...extra};
  if (token && !headers.Authorization && !headers.authorization) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

export async function apiRequest(url, options = {}) {
  const mergedOptions = {
    ...options,
    headers: authHeaders(options.headers || {}),
  };
  const response = await fetch(url, mergedOptions);
  const payload = await response.json();
  if (!response.ok) {
    const error = Error(payload.error || "请求暂不可用，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return payload;
}

export const SEVERITY_META = {
  high: {label: "高优先级", badge: "error", dot: "error"},
  medium: {label: "中优先级", badge: "warning", dot: "warning"},
  info: {label: "口径提示", badge: "info", dot: "accent"},
  ok: {label: "正常", badge: "success", dot: "success"},
};

export const DIMENSION_LABELS = {
  metering: "量测自洽",
  power_quality: "电能质量",
  system_balance: "运行方式与平衡",
  diurnal_baseline: "分时段基线",
};

export const DISPOSITION_META = {
  pending: {label: "待复核", badge: "neutral"},
  confirmed_anomaly: {label: "确认为异常", badge: "error"},
  normal_operation: {label: "正常生产/计划倒闸", badge: "success"},
  needs_site_check: {label: "待现场核查", badge: "warning"},
};

export const QUICK_DIAG_PROMPTS = [
  {
    id: "D01",
    tag: "单进线 · 规程",
    text: "2026年11月13日35kV站运行有哪些明显异常？是不是发生了单进线带双主变或故障？按煤矿安全规程应核查什么？",
  },
  {
    id: "D02",
    tag: "2#主变反向有功",
    text: "2026年11月12日12点2号主变通道（306/312/1006）发生了什么异常？可能原因有哪些？还缺哪些现场证据？",
  },
  {
    id: "D03",
    tag: "1018/1012 量测",
    text: "请检查1018柜和1012柜的量测数据是否健康？是否存在疑似接线或表计异常？",
  },
  {
    id: "D04",
    tag: "过压 & 低cos",
    text: "老洼地35kV站这7天10kV母线电压和井下出线功率因数整体健康状况如何？哪些回路风险最高？",
  },
  {
    id: "D05",
    tag: "轮询偏移甄别",
    text: "2026年11月16日17点303柜与311柜有功相差2400多kW，这是主变或母线电气故障吗？",
  },
  {
    id: "D06",
    tag: "LW-000010 基线",
    text: "2026年11月10日12点303柜有功突增3408kW（LW-000010），结合全站7天分时段基线看，这属于常规运行波动还是反常异常？",
  },
];
