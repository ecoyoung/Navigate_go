<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';

type User = {
  id: number;
  email: string;
  display_name: string;
  role: 'admin' | 'member';
  is_active: boolean;
  must_change_password: boolean;
};

type Topic = {
  id: number;
  name: string;
  intent_text: string;
  compiled_intent: { positive_keywords?: string[]; excluded_keywords?: string[] };
  cadence: 'realtime' | 'daily' | 'weekly';
  status: 'active' | 'paused' | 'draft';
  daily_credit_limit: number;
  match_count: number;
  candidate_source_count: number;
};

type FeedItem = {
  content_id: number;
  title: string;
  excerpt: string | null;
  paragraphs?: string[];
  source_name: string;
  url: string | null;
  published_at: string | null;
  discovered_at: string;
  topic_ids: number[];
  topic_names: string[];
  tags: string[];
  quality_tier: 'verified_full' | 'partial' | 'needs_enrichment';
  reader_eligible: boolean;
};

type DailyReportHistoryItem = {
  coverage_date: string;
  available_content_count: number;
};

type ReaderEventSource = {
  source_name: string;
  url: string | null;
};

type ReaderEventMember = {
  content_id: number;
  title: string;
  excerpt: string | null;
  paragraphs?: string[];
  source_name: string;
  url: string | null;
  published_at: string | null;
};

type ReaderEvent = {
  id: number;
  title: string;
  summary: string | null;
  paragraphs?: string[];
  first_published_at: string;
  last_published_at: string;
  source_count: number;
  member_count: number;
  sources: ReaderEventSource[];
  members: ReaderEventMember[];
};

type SourceCandidate = {
  id: number;
  title: string | null;
  host: string;
  canonical_url: string;
};

type SourcePath = {
  key: string;
  label: string;
  engine: string;
  status: string;
  url: string | null;
};

type CatalogSource = {
  id: number;
  catalog_id: string | null;
  name: string;
  channel_type: string;
  start_url: string;
  is_enabled: boolean;
  last_run_status: string | null;
  last_error_code: string | null;
  last_error_summary: string | null;
  last_fetched_count: number;
  last_new_count: number;
  last_skipped_count: number;
  last_rejected_count: number;
  circuit_open: boolean;
  execution_engine: string | null;
  viable_paths: SourcePath[];
};

type CrawlRun = {
  id: number;
  source_id: number;
  status: string;
  fetched_count: number;
  new_count: number;
  updated_count: number;
  skipped_count: number;
  rejected_count: number;
  error_code: string | null;
  error_summary: string | null;
};

const apiBase = import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '');
const user = ref<User | null>(null);
const topics = ref<Topic[]>([]);
const feed = ref<FeedItem[]>([]);
const selectedTopicId = ref<number | null>(null);
const view = ref<'for-you' | 'topics' | 'explore' | 'admin'>('for-you');
const accountOpen = ref(false);
const composerOpen = ref(false);
const loading = ref(true);
const busy = ref(false);
const message = ref('');
const error = ref('');

const authMode = ref<'login' | 'register'>('login');
const email = ref('');
const password = ref('');
const currentPassword = ref('');
const newPassword = ref('');

const topicName = ref('');
const topicIntent = ref('');
const topicKeywords = ref('');
const topicExclusions = ref('');
const topicCadence = ref<'daily' | 'weekly'>('daily');
const topicCreditLimit = ref(50);
const discovered = ref<SourceCandidate[]>([]);
const dailyReports = ref<DailyReportHistoryItem[]>([]);
const selectedReportDate = ref('');
const progress = ref({ active: false, percent: 0, stage: '' });
const adminUsers = ref<User[]>([]);
const adminAccount = ref('');
const adminPassword = ref('');
const adminSection = ref<'sites' | 'accounts'>('sites');
const catalogSources = ref<CatalogSource[]>([]);
const selectedSourceIds = ref<number[]>([]);
const newSourceUrl = ref('');
const newSourceName = ref('');
const exploreItems = ref<FeedItem[]>([]);
const events = ref<ReaderEvent[]>([]);
const openedItem = ref<FeedItem | null>(null);
const openedEvent = ref<ReaderEvent | null>(null);
const expandedEventId = ref<number | null>(null);
const searchQuery = ref('');
const searchResults = ref<FeedItem[] | null>(null);

const activeTopics = computed(() => topics.value.filter((topic) => topic.status === 'active'));
const selectedTopic = computed(() => topics.value.find((topic) => topic.id === selectedTopicId.value));
const readerReadyFeed = computed(() => feed.value.filter((item) => item.reader_eligible));
const readingItems = computed(() => {
  if (searchResults.value) return searchResults.value;
  if (view.value === 'explore') return exploreItems.value;
  return selectedTopic.value ? readerReadyFeed.value : feed.value;
});
const pageTitle = computed(() => {
  if (view.value === 'topics') return '我的主题';
  if (view.value === 'explore') return '探索';
  if (view.value === 'admin') return adminSection.value === 'sites' ? '网站' : '账号';
  return selectedTopic.value?.name || '为你精选';
});
const editionDate = computed(() =>
  new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' }).format(new Date()),
);

function storyIndex(index: number) {
  return String(index + 1).padStart(2, '0');
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { message?: string } | null;
    throw new Error(body?.message || '请求未完成');
  }
  return (response.status === 204 ? null : await response.json()) as T;
}

async function loadWorkspace() {
  if (!user.value) return;
  const loadedTopics = await api<Topic[]>('/api/v1/topics');
  topics.value = loadedTopics;
  await Promise.all([loadFeed(), loadExplore()]);
}

async function loadDailyReports(topicId: number | null = selectedTopicId.value) {
  dailyReports.value = [];
  selectedReportDate.value = '';
  if (!user.value || !topicId) return;
  const reports = await api<DailyReportHistoryItem[]>(`/api/v1/topics/${topicId}/daily-reports`);
  dailyReports.value = reports;
  if (reports.length) selectedReportDate.value = reports[0].coverage_date;
}

async function loadEvents(topicId: number | null = selectedTopicId.value) {
  if (!user.value) {
    events.value = [];
    return;
  }
  events.value = await api<ReaderEvent[]>(
    topicId ? `/api/v1/topics/${topicId}/events` : '/api/v1/feed/events',
  );
  expandedEventId.value = null;
}

function resetSearch() {
  searchQuery.value = '';
  searchResults.value = null;
}

async function loadFeed(topicId: number | null = selectedTopicId.value) {
  if (!user.value) return;
  feed.value = await api<FeedItem[]>(
    topicId ? `/api/v1/topics/${topicId}/feed` : '/api/v1/feed/for-you',
  );
  await loadEvents(topicId);
}

async function loadExplore() {
  if (!user.value) return;
  exploreItems.value = await api<FeedItem[]>('/api/v1/explore');
}

async function openExplore() {
  view.value = 'explore';
  resetSearch();
  await loadExplore();
}

async function selectTopic(id: number | null) {
  selectedTopicId.value = id;
  view.value = 'for-you';
  resetSearch();
  await Promise.all([loadFeed(id), loadDailyReports(id)]);
}

async function runSearch() {
  const query = searchQuery.value.trim();
  if (!query) {
    searchResults.value = null;
    return;
  }
  if (view.value === 'explore') {
    searchResults.value = await api<FeedItem[]>(`/api/v1/explore/search?q=${encodeURIComponent(query)}`);
    return;
  }
  searchResults.value = await api<FeedItem[]>(
    selectedTopicId.value
      ? `/api/v1/topics/${selectedTopicId.value}/search?q=${encodeURIComponent(query)}`
      : `/api/v1/feed/search?q=${encodeURIComponent(query)}`,
  );
}

function onSearchInput() {
  if (!searchQuery.value.trim()) searchResults.value = null;
}

async function toggleEvent(event: ReaderEvent) {
  openedItem.value = null;
  if (openedEvent.value?.id === event.id) {
    openedEvent.value = null;
    expandedEventId.value = null;
    return;
  }
  expandedEventId.value = event.id;
  let detailed = event;
  if (!event.members.length) {
    detailed = await api<ReaderEvent>(
      selectedTopicId.value
        ? `/api/v1/topics/${selectedTopicId.value}/events/${event.id}`
        : `/api/v1/feed/events/${event.id}`,
    );
    events.value = events.value.map((item) => (item.id === event.id ? detailed : item));
  }
  openedEvent.value = detailed;
}

function openCard(item: FeedItem) {
  openedEvent.value = null;
  openedItem.value = item;
}

function cardParagraphs(item: { paragraphs?: string[]; excerpt?: string | null; summary?: string | null }) {
  if (item.paragraphs?.length) return item.paragraphs;
  const fallback = item.excerpt || item.summary;
  return fallback ? [fallback] : [];
}

function topicRssUrl(topicId: number) {
  return `${apiBase}/api/v1/topics/${topicId}/rss.xml`;
}

function splitTerms(value: string) {
  return value
    .split(/[，,、\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function createTopic(autoDiscover = false) {
  if (!topicIntent.value.trim()) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await api<{ topic: Topic; items: FeedItem[] }>('/api/v1/topics', {
      method: 'POST',
      body: JSON.stringify({
        name: topicName.value.trim() || null,
        intent_text: topicIntent.value,
        keywords: splitTerms(topicKeywords.value),
        excluded_keywords: splitTerms(topicExclusions.value),
        cadence: topicCadence.value,
        daily_credit_limit: topicCreditLimit.value,
      }),
    });
    topics.value = [result.topic, ...topics.value];
    selectedTopicId.value = result.topic.id;
    feed.value = result.items;
    composerOpen.value = false;
    view.value = 'for-you';
    topicName.value = '';
    topicIntent.value = '';
    topicKeywords.value = '';
    topicExclusions.value = '';
    message.value = `已创建「${result.topic.name}」`;
    await loadDailyReports(result.topic.id);
    if (autoDiscover) await discoverSources(result.topic, false);
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '主题创建失败';
  } finally {
    busy.value = false;
  }
}

async function createTopicFromComposer() { await createTopic(false); }
async function createFirstTopic() { await createTopic(true); }

async function toggleTopic(topic: Topic) {
  const status = topic.status === 'active' ? 'paused' : 'active';
  const result = await api<{ topic: Topic }>(`/api/v1/topics/${topic.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  });
  topics.value = topics.value.map((item) => (item.id === topic.id ? result.topic : item));
  await loadFeed();
}

async function deleteTopic(topic: Topic) {
  if (!window.confirm(`删除「${topic.name}」？`)) return;
  busy.value = true;
  error.value = '';
  try {
    await api(`/api/v1/topics/${topic.id}`, { method: 'DELETE' });
    topics.value = topics.value.filter((item) => item.id !== topic.id);
    if (selectedTopicId.value === topic.id) {
      selectedTopicId.value = null;
      dailyReports.value = [];
      selectedReportDate.value = '';
      discovered.value = [];
    }
    await loadFeed();
    message.value = `主题“${topic.name}”已删除`;
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '主题删除失败';
  } finally {
    busy.value = false;
  }
}

async function discoverSources(topic: Topic, setBusy = true) {
  if (setBusy) busy.value = true;
  progress.value = { active: true, percent: 10, stage: '正在理解你的主题' };
  const stages = [
    ['正在搜索近 7 天来源', 28],
    ['正在解析网页与发布日期', 56],
    ['正在写入你的内容池', 80],
  ] as const;
  let stageIndex = 0;
  const ticker = window.setInterval(() => {
    if (stageIndex < stages.length) {
      const [stage, percent] = stages[stageIndex++];
      progress.value = { active: true, stage, percent };
    }
  }, 900);
  error.value = '';
  try {
    const result = await api<{
      candidates: SourceCandidate[];
      cache_hit: boolean;
      credits_used: number;
      fetched_pages: number;
      ingested_count: number;
      metadata_only_count: number;
      items: FeedItem[];
    }>(
      `/api/v1/topics/${topic.id}/discover`,
      { method: 'POST', body: JSON.stringify({ limit: 50 }) },
    );
    discovered.value = result.candidates;
    selectedTopicId.value = topic.id;
    feed.value = result.items;
    view.value = 'for-you';
    message.value = result.ingested_count ? `找到 ${result.ingested_count} 条` : '暂无新内容';
    topics.value = await api<Topic[]>('/api/v1/topics');
    await loadDailyReports(topic.id);
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '来源发现失败';
  } finally {
    window.clearInterval(ticker);
    progress.value = { active: true, percent: 100, stage: '内容已准备完成' };
    window.setTimeout(() => (progress.value.active = false), 900);
    if (setBusy) busy.value = false;
  }
}

async function loadAdminUsers() {
  adminUsers.value = await api<User[]>('/api/v1/admin/users');
}

function pathTone(status: string) {
  if (status === 'verified') return '已走通';
  if (status === 'failing') return '失败';
  if (status === 'blocked') return '停用';
  if (status === 'candidate' || status === 'draft') return '备选';
  return '待跑';
}

function summarizeRun(run: Pick<CrawlRun, 'status' | 'fetched_count' | 'new_count' | 'updated_count' | 'skipped_count' | 'rejected_count' | 'error_summary'>) {
  if (run.status === 'failed') return run.error_summary || '抓取失败';
  const parts = [`取回 ${run.fetched_count} 条`];
  if (run.new_count) parts.push(`新增 ${run.new_count} 条`);
  if (run.updated_count) parts.push(`更新 ${run.updated_count} 条`);
  if (run.skipped_count) parts.push(`已有 ${run.skipped_count} 条`);
  if (run.rejected_count) parts.push(`未收录 ${run.rejected_count} 条`);
  return parts.join('，');
}

function lastRunLabel(item: CatalogSource) {
  if (!item.last_run_status) return '尚未抓取';
  if (item.last_run_status === 'failed') return item.last_error_summary || '上次失败';
  return summarizeRun({
    status: item.last_run_status,
    fetched_count: item.last_fetched_count,
    new_count: item.last_new_count,
    updated_count: 0,
    skipped_count: item.last_skipped_count,
    rejected_count: item.last_rejected_count,
    error_summary: item.last_error_summary,
  });
}

function runFinished(status: string) {
  return status !== 'pending' && status !== 'running';
}

async function waitForRun(runId: number) {
  const [run] = await waitForRuns([runId]);
  return run;
}

async function waitForRuns(runIds: number[]) {
  const remaining = new Set(runIds);
  const results = new Map<number, CrawlRun>();
  for (let attempt = 0; attempt < 180; attempt += 1) {
    for (const runId of [...remaining]) {
      const run = await api<CrawlRun>(`/api/v1/crawl-runs/${runId}`);
      if (runFinished(run.status)) {
        remaining.delete(runId);
        results.set(runId, run);
      }
    }
    if (!remaining.size) return runIds.map((runId) => results.get(runId)!);
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  throw new Error('抓取仍在进行，请稍后刷新查看结果');
}

function sourceRunnable(item: CatalogSource) {
  return item.is_enabled && !item.circuit_open && !item.viable_paths.some((path) => path.status === 'blocked');
}

function toggleSourceSelection(id: number) {
  if (selectedSourceIds.value.includes(id)) {
    selectedSourceIds.value = selectedSourceIds.value.filter((item) => item !== id);
    return;
  }
  selectedSourceIds.value = [...selectedSourceIds.value, id];
}

async function loadCatalogSources() {
  if (user.value?.role !== 'admin') return;
  catalogSources.value = await api<CatalogSource[]>('/api/v1/sources?family=website');
  selectedSourceIds.value = selectedSourceIds.value.filter((id) =>
    catalogSources.value.some((item) => item.id === id),
  );
}

async function openAdminWorkspace(section: 'sites' | 'accounts' = adminSection.value) {
  if (user.value?.role !== 'admin') return;
  adminSection.value = section;
  view.value = 'admin';
  error.value = '';
  if (section === 'accounts') {
    await loadAdminUsers();
    return;
  }
  await loadCatalogSources();
}

async function addCatalogSource() {
  if (!newSourceUrl.value.trim()) return;
  busy.value = true;
  error.value = '';
  try {
    const created = await api<CatalogSource>('/api/v1/sources', {
      method: 'POST',
      body: JSON.stringify({
        name: newSourceName.value.trim() || null,
        start_url: newSourceUrl.value.trim(),
        probe: true,
      }),
    });
    newSourceUrl.value = '';
    newSourceName.value = '';
    message.value = `已加入 ${created.name}`;
    await loadCatalogSources();
  } catch (err) {
    error.value = err instanceof Error ? err.message : '未能加入网站';
  } finally {
    busy.value = false;
  }
}

async function crawlSelectedSources() {
  const ids = selectedSourceIds.value.filter((id) => {
    const item = catalogSources.value.find((source) => source.id === id);
    return item ? sourceRunnable(item) : false;
  });
  if (!ids.length) return;
  busy.value = true;
  error.value = '';
  try {
    const accepted = await api<{ run_id: number }[]>('/api/v1/sources/crawl-selected', {
      method: 'POST',
      body: JSON.stringify({ source_ids: ids }),
    });
    const runs = await waitForRuns(accepted.map((item) => item.run_id));
    const added = runs.reduce((sum, run) => sum + run.new_count, 0);
    const fetched = runs.reduce((sum, run) => sum + run.fetched_count, 0);
    message.value = `已抓取 ${runs.length} 个网站，取回 ${fetched} 条，新增 ${added} 条`;
    selectedSourceIds.value = [];
    await Promise.all([loadCatalogSources(), loadExplore()]);
  } catch (err) {
    error.value = err instanceof Error ? err.message : '未能开始抓取';
  } finally {
    busy.value = false;
  }
}

async function crawlOneSource(item: CatalogSource) {
  if (!sourceRunnable(item)) return;
  busy.value = true;
  error.value = '';
  try {
    const accepted = await api<{ run_id: number }>(`/api/v1/sources/${item.id}/crawl`, { method: 'POST' });
    const run = await waitForRun(accepted.run_id);
    message.value = `${item.name}：${summarizeRun(run)}`;
    await Promise.all([loadCatalogSources(), loadExplore()]);
  } catch (err) {
    error.value = err instanceof Error ? err.message : '未能开始抓取';
  } finally {
    busy.value = false;
  }
}

function retireMessage(deleted: number, hidden: number) {
  const total = deleted + hidden;
  return total ? `已从列表移除 ${total} 个网站` : '未移除网站';
}

async function deleteSelectedSources() {
  const ids = selectedSourceIds.value.filter((id) =>
    catalogSources.value.some((item) => item.id === id),
  );
  if (!ids.length) return;
  if (!window.confirm(`从列表移除这 ${ids.length} 个网站？已取回的内容会保留。`)) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await api<{ deleted: number; hidden: number }>('/api/v1/sources/delete-selected', {
      method: 'POST',
      body: JSON.stringify({ source_ids: ids }),
    });
    selectedSourceIds.value = [];
    message.value = retireMessage(result.deleted, result.hidden);
    await loadCatalogSources();
  } catch (err) {
    error.value = err instanceof Error ? err.message : '未能移除网站';
  } finally {
    busy.value = false;
  }
}

async function deleteOneSource(item: CatalogSource) {
  if (!window.confirm(`从列表移除「${item.name}」？已取回的内容会保留。`)) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await api<{ deleted: number; hidden: number }>(`/api/v1/sources/${item.id}`, {
      method: 'DELETE',
    });
    selectedSourceIds.value = selectedSourceIds.value.filter((id) => id !== item.id);
    message.value = retireMessage(result.deleted, result.hidden);
    await loadCatalogSources();
  } catch (err) {
    error.value = err instanceof Error ? err.message : '未能移除网站';
  } finally {
    busy.value = false;
  }
}

async function toggleSourceEnabled(item: CatalogSource) {
  busy.value = true;
  try {
    await api<CatalogSource>(`/api/v1/sources/${item.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_enabled: !item.is_enabled }),
    });
    await loadCatalogSources();
  } catch (err) {
    error.value = err instanceof Error ? err.message : '未能更新网站';
  } finally {
    busy.value = false;
  }
}

async function createManagedUser() {
  busy.value = true;
  try {
    await api<User>('/api/v1/admin/users', {
      method: 'POST',
      body: JSON.stringify({ account: adminAccount.value, temporary_password: adminPassword.value }),
    });
    adminAccount.value = ''; adminPassword.value = '';
    await loadAdminUsers();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '账号创建失败';
  } finally { busy.value = false; }
}

async function toggleManagedUser(item: User) {
  await api<User>(`/api/v1/admin/users/${item.id}`, {
    method: 'PATCH', body: JSON.stringify({ is_active: !item.is_active }),
  });
  await loadAdminUsers();
}

async function submitAuth() {
  busy.value = true;
  error.value = '';
  try {
    const path = authMode.value === 'login' ? '/api/v1/auth/login' : '/api/v1/auth/register';
    const body =
      authMode.value === 'login'
        ? { account: email.value, password: password.value }
        : { account: email.value, password: password.value };
    const result = await api<{ user: User }>(path, { method: 'POST', body: JSON.stringify(body) });
    user.value = result.user;
    password.value = '';
    await loadWorkspace();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '登录失败';
  } finally {
    busy.value = false;
  }
}

async function changePassword() {
  busy.value = true;
  error.value = '';
  try {
    await api('/api/v1/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword.value, new_password: newPassword.value }),
    });
    user.value = null;
    topics.value = [];
    feed.value = [];
    currentPassword.value = '';
    newPassword.value = '';
    message.value = '密码已更新，请重新登录';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '密码更新失败';
  } finally {
    busy.value = false;
  }
}

async function logout() {
  await api('/api/v1/auth/logout', { method: 'POST' });
  user.value = null;
  topics.value = [];
  feed.value = [];
  events.value = [];
  exploreItems.value = [];
  dailyReports.value = [];
  selectedReportDate.value = '';
  resetSearch();
  accountOpen.value = false;
}

function shortDate(value: string | null) {
  if (!value) return '日期待核';
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(new Date(value));
}

function dailyReportUrl() {
  return selectedReportDate.value && selectedTopicId.value
    ? `${apiBase}/api/v1/topics/${selectedTopicId.value}/daily-reports/${selectedReportDate.value}`
    : '#';
}

onMounted(async () => {
  try {
    user.value = await api<User>('/api/v1/auth/me');
    await loadWorkspace();
  } catch {
    user.value = null;
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <main v-if="loading" class="loading-screen">
    <div class="brand-lockup">
      <img src="/brand/logo/mark-wine-24.svg" width="28" height="28" alt="" />
      <strong>Navigate</strong>
    </div>
    <span>打开中</span>
  </main>

  <main v-else-if="!user" class="auth-page">
    <section class="login-card">
      <header class="paper-masthead">
        <img src="/brand/logo/mark-wine-24.svg" width="36" height="36" alt="" />
        <strong>Navigate</strong>
        <p class="kicker">纸面编辑室</p>
        <p class="edition">{{ editionDate }}</p>
      </header>
      <h1>{{ authMode === 'login' ? '登录' : '注册' }}</h1>
      <form class="account-form" @submit.prevent="submitAuth">
        <input v-model="email" placeholder="账户名" required />
        <input v-model="password" type="password" placeholder="密码" :minlength="authMode === 'register' ? 12 : 1" required />
        <button class="primary-button" :disabled="busy">{{ busy ? '处理中' : authMode === 'login' ? '登录' : '注册' }}</button>
      </form>
      <button class="text-button" @click="authMode = authMode === 'login' ? 'register' : 'login'; error = ''">{{ authMode === 'login' ? '还没有账号？注册' : '已有账号？登录' }}</button>
      <p v-if="error" class="modal-error">{{ error }}</p>
    </section>
  </main>

  <main v-else-if="!topics.length" class="onboarding-page">
    <section class="onboarding-sheet">
      <header class="paper-masthead compact">
        <img src="/brand/logo/mark-wine-24.svg" width="28" height="28" alt="" />
        <strong>Navigate</strong>
      </header>
      <p class="kicker">第一份订阅</p>
      <h1>你想持续关注什么</h1>
      <form @submit.prevent="createFirstTopic">
        <textarea v-model="topicIntent" rows="4" placeholder="中国消费品牌出海东南亚，排除促销软文" required />
        <button class="primary-button" :disabled="busy">{{ busy ? '创建中' : '创建' }}</button>
      </form>
      <div class="examples"><span>具身智能融资</span><span>消费品牌出海</span><span>防晒新品研发</span></div>
    </section>
    <section v-if="progress.active" class="collection-progress"><p>{{ progress.stage }}</p><div><i :style="{ width: `${progress.percent}%` }" /></div><small>{{ progress.percent }}%</small></section>
    <p v-if="error" class="modal-error">{{ error }}</p>
  </main>

  <main v-else :class="['intel-shell', { 'admin-shell': view === 'admin' }]">
    <aside class="sidebar">
      <a class="brand brand-lockup" href="#" @click.prevent="selectTopic(null)">
        <img src="/brand/logo/mark-wine-24.svg" width="22" height="22" alt="" />
        <strong>Navigate</strong>
      </a>
      <nav class="side-nav" aria-label="主要功能">
        <p>目录</p>
        <button :class="{ active: view === 'for-you' && !selectedTopicId }" @click="selectTopic(null)">
          <i class="icon icon-featured" aria-hidden="true" /> 为你精选
        </button>
        <button :class="{ active: view === 'topics' }" @click="view = 'topics'">
          <i class="icon icon-topics" aria-hidden="true" /> 我的主题 <b>{{ topics.length }}</b>
        </button>
        <button :class="{ active: view === 'explore' }" @click="openExplore">
          <i class="icon icon-explore" aria-hidden="true" /> 探索
        </button>
        <button v-if="user.role === 'admin'" :class="{ active: view === 'admin' }" @click="openAdminWorkspace('sites')">
          <i class="icon icon-settings" aria-hidden="true" /> 管理
        </button>
        <a
          v-if="user && selectedTopic && selectedReportDate"
          class="nav-link"
          :href="dailyReportUrl()"
          target="_blank"
          rel="noreferrer"
        >
          <i class="icon icon-daily" aria-hidden="true" /> 每日简报
        </a>
        <button v-else disabled>
          <i class="icon icon-daily" aria-hidden="true" /> 每日简报
        </button>
      </nav>

      <section v-if="user" class="topic-shortcuts">
        <p>我的主题</p>
        <button
          v-for="topic in topics.slice(0, 6)"
          :key="topic.id"
          :class="{ active: selectedTopicId === topic.id }"
          @click="selectTopic(topic.id)"
        >
          <i :class="topic.status" />
          <span>{{ topic.name }}</span>
          <b>{{ topic.match_count }}</b>
        </button>
      </section>

      <button class="account-chip" type="button" @click="accountOpen = true">
        <span>{{ user ? user.display_name.slice(0, 1) : '访' }}</span>
        <div><b>{{ user?.display_name || '登录账号' }}</b><small>{{ user?.email || '创建你的情报订阅' }}</small></div>
      </button>
    </aside>

    <section class="content-area">
      <header class="content-header">
        <div>
          <p>纸面编辑室 · {{ editionDate }}</p>
          <h1>{{ pageTitle }}</h1>
        </div>
        <div class="header-actions">
          <form v-if="view === 'for-you' || view === 'explore'" class="paper-search" @submit.prevent="runSearch">
            <i class="icon icon-search" aria-hidden="true" />
            <input
              v-model="searchQuery"
              :placeholder="view === 'explore' ? '检索目录' : '检索本期'"
              @input="onSearchInput"
            />
          </form>
          <button v-if="user" class="new-topic" type="button" @click="composerOpen = true">
            <i class="icon icon-new" aria-hidden="true" /> 新建主题
          </button>
          <button v-else class="new-topic" type="button" @click="accountOpen = true">登录后订阅</button>
        </div>
      </header>

      <div v-if="message" class="notice success" @click="message = ''">{{ message }}</div>
      <div v-if="error" class="notice error" @click="error = ''">{{ error }}</div>

      <section v-if="view === 'topics'" class="topic-grid">
        <button class="topic-card create-card" type="button" @click="composerOpen = true">
          <i class="icon icon-new" aria-hidden="true" />
          <span>新主题</span>
        </button>
        <article v-for="topic in topics" :key="topic.id" class="topic-card">
          <div class="topic-card-top"><i :class="topic.status" /><span>{{ topic.status === 'active' ? '持续更新' : '已暂停' }}</span></div>
          <h2>{{ topic.name }}</h2>
          <p>{{ topic.intent_text }}</p>
          <div class="topic-stats"><span><b>{{ topic.match_count }}</b> 篇</span></div>
          <div class="topic-actions">
            <button @click="selectTopic(topic.id)">查看内容</button>
            <a class="rss-link" :href="topicRssUrl(topic.id)" target="_blank" rel="noreferrer">RSS</a>
            <button @click="discoverSources(topic)"><i class="icon icon-source" aria-hidden="true" />发现来源</button>
            <button @click="toggleTopic(topic)">{{ topic.status === 'active' ? '暂停' : '恢复' }}</button>
            <button class="danger-action" :disabled="busy" @click="deleteTopic(topic)"><i class="icon icon-delete" aria-hidden="true" />删除</button>
          </div>
        </article>
      </section>

      <section v-else-if="view === 'admin' && user.role === 'admin'" class="admin-workspace">
        <div class="admin-tabs" role="tablist" aria-label="管理分区">
          <button :class="{ active: adminSection === 'sites' }" @click="openAdminWorkspace('sites')">网站</button>
          <button :class="{ active: adminSection === 'accounts' }" @click="openAdminWorkspace('accounts')">账号</button>
        </div>
        <template v-if="adminSection === 'sites'">
          <div class="stream-heading"><div><h2>网站</h2></div><span>{{ catalogSources.length }}</span></div>
          <section class="admin-create-panel">
            <div>
              <p class="kicker">来源</p>
              <h3>补充网站</h3>
              <p>加入后进入每日两次采集。路径按入口识别。</p>
            </div>
            <form class="account-form" @submit.prevent="addCatalogSource">
              <input v-model="newSourceUrl" type="url" placeholder="https://example.com/feed.xml" required />
              <input v-model="newSourceName" placeholder="名称，可留空" />
              <button class="primary-button" :disabled="busy">{{ busy ? '正在加入' : '加入网站' }}</button>
            </form>
          </section>
          <div class="source-toolbar">
            <button class="ghost-button" :disabled="busy || !selectedSourceIds.length" @click="crawlSelectedSources">{{ busy ? '正在抓取' : '抓取所选' }}</button>
            <button class="ghost-button danger-action" :disabled="busy || !selectedSourceIds.length" @click="deleteSelectedSources">删除所选</button>
            <small>{{ selectedSourceIds.length ? `已选 ${selectedSourceIds.length} 个` : '勾选后可抓取或移出列表' }}</small>
          </div>
          <section class="managed-users-table source-table" aria-label="网站列表">
            <article v-for="item in catalogSources" :key="item.id" class="source-card">
              <label class="source-check">
                <input
                  type="checkbox"
                  :checked="selectedSourceIds.includes(item.id)"
                  @change="toggleSourceSelection(item.id)"
                />
              </label>
              <div class="source-main">
                <div class="source-title">
                  <b>{{ item.name }}</b>
                  <span :class="item.is_enabled ? 'status-active' : 'status-paused'">
                    {{ item.circuit_open ? '熔断' : item.last_run_status === 'failed' ? '失败' : item.is_enabled ? '启用中' : '已停用' }}
                  </span>
                </div>
                <small class="source-url"><a :href="item.start_url" target="_blank" rel="noreferrer">{{ item.start_url }}</a></small>
                <span class="path-list">
                  <i v-for="path in item.viable_paths" :key="path.key + path.url" :class="['path-pill', path.status]">
                    {{ path.label }} · {{ pathTone(path.status) }}
                  </i>
                  <small v-if="!item.viable_paths.length">未识别</small>
                </span>
                <small class="source-run">{{ lastRunLabel(item) }}</small>
              </div>
              <div class="source-actions">
                <button :disabled="busy || !sourceRunnable(item)" @click="crawlOneSource(item)">{{ busy ? '抓取中' : '抓取' }}</button>
                <button :disabled="busy" @click="toggleSourceEnabled(item)">{{ item.is_enabled ? '停用' : '启用' }}</button>
                <button class="danger-action" :disabled="busy" @click="deleteOneSource(item)">删除</button>
              </div>
            </article>
          </section>
        </template>
        <template v-else>
          <div class="stream-heading"><div><h2>账号</h2></div><span>{{ adminUsers.length }}</span></div>
          <section class="admin-create-panel">
            <div><p class="kicker">账号</p><h3>创建账号</h3><p>临时密码，首次登录后需修改。</p></div>
            <form class="account-form" @submit.prevent="createManagedUser">
              <input v-model="adminAccount" placeholder="账户名" required />
              <input v-model="adminPassword" type="password" minlength="12" placeholder="临时密码（至少 12 位）" required />
              <button class="primary-button" :disabled="busy">{{ busy ? '正在创建' : '创建账号' }}</button>
            </form>
          </section>
          <section class="managed-users-table" aria-label="账号列表">
            <div class="managed-user managed-user-header"><span>账户</span><span>角色</span><span>状态</span><span>操作</span></div>
            <div v-for="item in adminUsers" :key="item.id" class="managed-user">
              <span><b>{{ item.email }}</b><small>{{ item.display_name }}</small></span>
              <span>{{ item.role === 'admin' ? '管理员' : '订阅读者' }}</span>
              <span :class="item.is_active ? 'status-active' : 'status-paused'">{{ item.is_active ? '启用中' : '已停用' }}</span>
              <button :disabled="item.id === user.id" @click="toggleManagedUser(item)">{{ item.is_active ? '停用' : '恢复' }}</button>
            </div>
          </section>
        </template>
      </section>

      <section v-else-if="view === 'explore'" class="stream-section">
        <div class="stream-heading"><div><h2>{{ searchResults ? '检索结果' : '探索' }}</h2></div><span>{{ readingItems.length }}</span></div>
        <article
          v-for="(item, index) in readingItems"
          :key="item.content_id"
          class="intel-card"
          role="button"
          tabindex="0"
          @click="openCard(item)"
          @keydown.enter.prevent="openCard(item)"
        >
          <span class="story-index">{{ storyIndex(index) }}</span>
          <div class="meta"><span>{{ item.source_name }}</span><time>{{ shortDate(item.published_at) }}</time></div>
          <h2>{{ item.title }}</h2>
          <p v-for="(paragraph, paragraphIndex) in cardParagraphs(item)" :key="`${item.content_id}-${paragraphIndex}`">{{ paragraph }}</p>
          <div class="topic-tags"><span v-for="tag in item.tags" :key="tag">{{ tag }}</span></div>
          <a
            v-if="item.url"
            class="origin-link"
            :href="item.url ?? undefined"
            target="_blank"
            rel="noreferrer"
            @click.stop
          >原文 · {{ item.source_name }}</a>
        </article>
        <div v-if="!readingItems.length" class="empty-workspace compact"><h2>{{ searchResults ? '暂无匹配' : '暂无内容' }}</h2></div>
      </section>

      <section v-else class="stream-section">
        <div v-if="user && !topics.length" class="empty-workspace">
          <h2>还没有主题</h2><button @click="composerOpen = true">创建</button>
        </div>
        <template v-else>
          <section v-if="!searchResults" class="event-board">
            <div class="stream-heading"><div><h2>今日事件</h2></div><span>{{ events.length }}</span></div>
            <article
              v-for="event in events"
              :key="event.id"
              class="event-card"
              :class="{ open: expandedEventId === event.id }"
            >
              <button type="button" class="event-masthead" @click="toggleEvent(event)">
                <div class="meta">
                  <span>{{ event.source_count }} 家来源</span>
                  <time>{{ shortDate(event.last_published_at) }}</time>
                </div>
                <h3>{{ event.title }}</h3>
                <p v-for="(paragraph, paragraphIndex) in cardParagraphs(event)" :key="`${event.id}-${paragraphIndex}`">{{ paragraph }}</p>
              </button>
              <div class="event-sources">
                <span v-for="source in event.sources" :key="`${event.id}-${source.source_name}-${source.url || ''}`">{{ source.source_name }}</span>
              </div>
            </article>
            <div v-if="!events.length" class="empty-workspace compact"><h2>暂无事件</h2></div>
          </section>
          <div class="stream-heading"><div><h2>{{ searchResults ? '检索结果' : '精选' }}</h2></div><span>{{ readingItems.length }}</span></div>
          <article
            v-for="(item, index) in readingItems"
            :key="item.content_id"
            class="intel-card"
            role="button"
            tabindex="0"
            @click="openCard(item)"
            @keydown.enter.prevent="openCard(item)"
          >
            <span class="story-index">{{ storyIndex(index) }}</span>
            <div class="meta"><span>{{ item.source_name }}</span><time>{{ shortDate(item.published_at) }}</time></div>
            <h2>{{ item.title }}</h2>
            <p v-for="(paragraph, paragraphIndex) in cardParagraphs(item)" :key="`${item.content_id}-${paragraphIndex}`">{{ paragraph }}</p>
            <div class="topic-tags">
              <span v-for="name in item.topic_names" :key="`topic-${name}`">{{ name }}</span>
              <span v-for="tag in item.tags" :key="`tag-${tag}`">{{ tag }}</span>
            </div>
            <a
              v-if="item.url"
              class="origin-link"
              :href="item.url ?? undefined"
              target="_blank"
              rel="noreferrer"
              @click.stop
            >原文 · {{ item.source_name }}</a>
          </article>
          <div v-if="user && topics.length && !readingItems.length" class="empty-workspace compact"><h2>暂无匹配</h2></div>
        </template>
      </section>
    </section>

    <aside v-if="view !== 'admin'" class="context-panel">
      <section><p>今日</p><div class="overview-number">{{ searchResults ? readingItems.length : (selectedTopic ? readerReadyFeed.length : feed.length) }}</div><span>{{ searchResults ? '条' : '篇' }}</span></section>
      <section><p>主题</p><div class="mini-list"><button v-for="topic in activeTopics" :key="topic.id" @click="selectTopic(topic.id)"><i class="active" /><span>{{ topic.name }}</span><b>{{ topic.match_count }}</b></button><small v-if="!activeTopics.length">暂无</small></div></section>
      <section v-if="view === 'topics'" class="budget-card"><p>额度</p><strong>{{ selectedTopic?.daily_credit_limit ?? 0 }}</strong><span>每日</span></section>
      <section v-if="user && selectedTopic" class="daily-history"><p>日报</p><select v-if="dailyReports.length" v-model="selectedReportDate"><option v-for="report in dailyReports" :key="report.coverage_date" :value="report.coverage_date">{{ report.coverage_date }} · {{ report.available_content_count }} 条</option></select><a v-if="selectedReportDate" :href="dailyReportUrl()" target="_blank" rel="noreferrer"><i class="icon icon-daily" aria-hidden="true" />查看</a><small v-else>暂无</small></section>
      <section v-if="view === 'topics' && discovered.length"><p>最近发现</p><a v-for="item in discovered.slice(0, 5)" :key="item.id" :href="item.canonical_url" target="_blank" rel="noreferrer"><span>{{ item.title || item.host }}</span><small>{{ item.host }}</small></a></section>
    </aside>

    <nav class="mobile-nav" aria-label="移动导航">
      <button :class="{ active: view === 'for-you' && !selectedTopicId }" @click="selectTopic(null)">
        <i class="icon icon-featured" aria-hidden="true" />精选
      </button>
      <button :class="{ active: view === 'topics' }" @click="view = 'topics'">
        <i class="icon icon-topics" aria-hidden="true" />主题
      </button>
      <button :class="{ active: view === 'explore' }" @click="openExplore">
        <i class="icon icon-explore" aria-hidden="true" />探索
      </button>
      <a
        v-if="selectedTopic && selectedReportDate"
        :href="dailyReportUrl()"
        target="_blank"
        rel="noreferrer"
      >
        <i class="icon icon-daily" aria-hidden="true" />日报
      </a>
      <button v-else disabled>
        <i class="icon icon-daily" aria-hidden="true" />日报
      </button>
    </nav>

    <div v-if="openedItem" class="modal-backdrop" @click.self="openedItem = null">
      <section class="reading-sheet" role="dialog" aria-modal="true" :aria-label="openedItem.title">
        <button class="modal-close" @click="openedItem = null">×</button>
        <p class="kicker">{{ openedItem.source_name }}</p>
        <h2>{{ openedItem.title }}</h2>
        <time>{{ shortDate(openedItem.published_at) }}</time>
        <p v-for="(paragraph, paragraphIndex) in cardParagraphs(openedItem)" :key="`open-${openedItem.content_id}-${paragraphIndex}`">{{ paragraph }}</p>
        <div class="topic-tags">
          <span v-for="tag in openedItem.tags" :key="`open-tag-${tag}`">{{ tag }}</span>
        </div>
        <a
          v-if="openedItem.url"
          class="origin-link"
          :href="openedItem.url ?? undefined"
          target="_blank"
          rel="noreferrer"
        >阅读原文</a>
      </section>
    </div>
    <div v-if="openedEvent" class="modal-backdrop" @click.self="openedEvent = null">
      <section class="reading-sheet" role="dialog" aria-modal="true" :aria-label="openedEvent.title">
        <button class="modal-close" @click="openedEvent = null">×</button>
        <p class="kicker">{{ openedEvent.source_count }} 家来源</p>
        <h2>{{ openedEvent.title }}</h2>
        <time>{{ shortDate(openedEvent.last_published_at) }}</time>
        <p v-for="(paragraph, paragraphIndex) in cardParagraphs(openedEvent)" :key="`event-open-${openedEvent.id}-${paragraphIndex}`">{{ paragraph }}</p>
        <div class="event-members">
          <article v-for="member in openedEvent.members" :key="member.content_id">
            <small>{{ member.source_name }}</small>
            <strong>{{ member.title }}</strong>
            <p v-for="(paragraph, paragraphIndex) in cardParagraphs(member)" :key="`member-${member.content_id}-${paragraphIndex}`">{{ paragraph }}</p>
            <a
              v-if="member.url"
              class="origin-link"
              :href="member.url ?? undefined"
              target="_blank"
              rel="noreferrer"
            >原文</a>
          </article>
        </div>
      </section>
    </div>
    <div v-if="composerOpen" class="modal-backdrop" @click.self="composerOpen = false">
      <section class="topic-composer" role="dialog" aria-modal="true" aria-label="创建主题">
        <button class="modal-close" @click="composerOpen = false">×</button>
        <p class="kicker">新主题</p><h2>你想持续关注什么</h2>
        <form @submit.prevent="createTopicFromComposer">
          <label>描述<textarea v-model="topicIntent" rows="4" placeholder="中国消费品牌出海东南亚，排除促销软文" required /></label>
          <div class="form-grid"><label>名称<input v-model="topicName" placeholder="可留空" /></label><label>频率<select v-model="topicCadence"><option value="daily">每日</option><option value="weekly">每周</option></select></label></div>
          <label>关键词<input v-model="topicKeywords" placeholder="东南亚，出海" /></label>
          <label>排除<input v-model="topicExclusions" placeholder="促销软文" /></label>
          <div class="credit-row"><span>每日发现上限</span><input v-model.number="topicCreditLimit" type="number" min="0" max="100" /><small>额度</small></div>
          <button class="primary-button" type="submit" :disabled="busy">{{ busy ? '创建中' : '创建' }}</button>
        </form>
      </section>
    </div>

    <div v-if="accountOpen" class="modal-backdrop" @click.self="accountOpen = false">
      <section class="account-modal" role="dialog" aria-modal="true" aria-label="账号">
        <button class="modal-close" @click="accountOpen = false">×</button>
        <template v-if="user">
          <p class="kicker">账号</p><h2>{{ user.display_name }}</h2><p class="muted">{{ user.email }} · {{ user.role === 'admin' ? '管理员' : '订阅读者' }}</p>
          <form v-if="user.must_change_password" class="account-form" @submit.prevent="changePassword"><p>首次登录请更换临时密码。</p><input v-model="currentPassword" type="password" placeholder="当前临时密码" required /><input v-model="newPassword" type="password" minlength="12" placeholder="新密码（至少 12 位）" required /><button class="primary-button" :disabled="busy">更新密码</button></form>
          <button class="text-button" @click="logout">退出登录</button>
          <button v-if="user.role === 'admin'" class="text-button" @click="accountOpen = false; openAdminWorkspace('accounts')">前往账号管理</button>
        </template>
        <template v-else>
          <p class="kicker">纸面编辑室</p><h2>{{ authMode === 'login' ? '登录' : '注册' }}</h2>
          <form class="account-form" @submit.prevent="submitAuth"><input v-model="email" placeholder="账户名" required /><input v-model="password" type="password" placeholder="密码" :minlength="authMode === 'register' ? 12 : 1" required /><button class="primary-button" :disabled="busy">{{ busy ? '处理中' : authMode === 'login' ? '登录' : '注册' }}</button></form>
          <button class="text-button" @click="authMode = authMode === 'login' ? 'register' : 'login'; error = ''">{{ authMode === 'login' ? '还没有账号？注册' : '已有账号？登录' }}</button>
        </template>
        <p v-if="error" class="modal-error">{{ error }}</p>
      </section>
    </div>
  </main>
</template>
