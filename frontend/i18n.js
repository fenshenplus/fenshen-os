/* 分身 i18n（v6.4.16）——渐进式国际化，默认中文完全不变
 * 语言优先级：URL ?lang=en|zh > localStorage.fs_lang > 浏览器语言(en*→en，其余→zh)
 * 用法：
 *   静态节点：<span data-i18n="登录">登录</span>（en 时替换；zh/缺词自动回退原文）
 *   占位符：  <input data-i18n-ph="手机号">
 *   JS 动态： __t('写一个落地页')  返回当前语言文案
 * 语言包字典缺失时回退原文（中文），保证不破坏中文版。
 */
(function () {
  var DICT = {
    en: {
      // ── 登录 / 注册（国际用户第一入口）──
      "分身账号": "Fenshen Account",
      "邮箱（国际）或手机号（国内）注册登录；登录后可使用大模型 API 与云端能力（账号本地保存，登录状态长期保持）": "Sign in with email (global) or phone (CN). Your account is stored locally and stays signed in.",
      "登录": "Log in",
      "注册": "Sign up",
      "邮箱或手机号": "Email or phone",
      "短信验证码": "SMS code",
      "密码（邮箱≥8位 · 手机号≥6位）": "Password (≥8 for email · ≥6 for phone)",
      "密码（至少 8 位）": "Password (min 8 chars)",
      "密码（至少 6 位）": "Password (min 6 chars)",
      "暂不登录（本地模式）": "Skip for now (local mode)",
      "已登录": "Signed in",
      "注册成功，已登录": "Signed up, welcome!",
      "已退出登录": "Signed out",
      // ── 顶部导航 / 侧栏 ──
      "账户与设置": "Account & Settings",
      "进程轨迹": "Timeline",
      "看板": "Board",
      "模版看板": "Template Board",
      "发送": "Send",
      "请输入消息…": "Type a message…",
      // ── 工作台 ──
      "元神 · 我的分身": "Meta-Agent · My Digital Twin",
      "元神驾驶舱": "Meta Cockpit",
      "项目看板": "Project Board",
      "角色库": "Role Library",
      "公共资源库": "Shared Resources",
      "模型设置": "Model Settings",
      "终端": "Terminal",
      "浏览器": "Browser",
      "清理中心": "Cleanup Center",
      "长期记忆": "Long-term Memory",
      "技能库": "Skill Library",
      "经验库": "Experience Base",
      "进化中心": "Evolution Center",
      "元神续航": "Autonomy",
      "关于分身": "About",
      "设置": "Settings",
      // ── 驾驶舱 ──
      "元神状态": "Meta State",
      "在岗成员": "Active Members",
      "关键路径": "Critical Path",
      "今日派单": "Today Dispatched",
      "调度 token": "Dispatch tok",
      "执行 token": "Exec tok",
      "其他 token": "Other tok",
      "休息窗": "Rest Window",
      "模式": "Mode",
      "晨报": "Morning Brief",
      "元神汇报": "Meta Report",
      "战队 · 团队成员": "Team Members",
      "+ 成员": "+ Member",
      "💬 私聊元神": "💬 Chat Meta",
      "我的产品": "My Products",
      "还没有产品。去「对话」说一句\"做个 XX 产品\"即可建项。": "No products yet. Say \"build an XX product\" in Chat to create one.",
      // ── 看板 ──
      "完成度": "Progress",
      "阶段：": "Phase: ",
      "⏸ 暂停自主推进": "⏸ Pause Autonomy",
      "▶ 恢复自主推进": "▶ Resume Autonomy",
      "🚀 继续推进": "🚀 Continue",
      "🚀 部署": "🚀 Deploy",
      "⚙ 档位·自动": "⚙ Tier·Auto",
      "⚡ 档位·即时直答": "⚡ Tier·Instant",
      "🚀 档位·单角色速办": "🚀 Tier·Solo",
      "👥 档位·团队协作": "👥 Tier·Team",
      // ── 设置 ──
      "元神设置": "Meta Settings",
      "保存设置": "Save Settings",
      "开启": "On",
      "关闭": "Off",
      // ── 群聊设置 ──
      "群聊设置": "Group Settings",
      "项目目标": "Project Goal",
      "完成标准 / 当前阶段": "Acceptance / Phase",
      "团队成员": "Team Members",
      "暂无成员": "No members",
      // ── 通用 ──
      "取消": "Cancel",
      "确认": "Confirm",
      "删除": "Delete",
      "编辑": "Edit",
      "新建": "New",
      "名称": "Name",
      "描述": "Description",
      "搜索…": "Search…",
      "加载中…": "Loading…",
      "暂无数据": "No data",
      "操作成功": "Success",
      "操作失败": "Failed",
      "请求失败：": "Request failed: ",
      "先选择项目": "Select a project first",
      "已设置档位：": "Tier set: ",
      "自动判定": "Auto",
      "即时直答（最省token）": "Instant answer",
      "单角色速办": "Single-role",
      "团队协作（质量优先）": "Team (quality)",
      // ── 蒸馏 / 元神 ──
      "蒸馏": "Distill",
      "了解": "About",
      "蒸馏给人": "Clone for",
      "设定档": "Profile",
      "管理员体检": "Admin Check",
      "总看板": "Dashboard",
      // ── 进化中心 ──
      "生盘复盘": "Auto Review",
      "手动复盘": "Manual Review",
      "待确认": "Pending",
      "全部": "All",
      "生成复盘": "Generate Review",
      "元神记忆面板": "Meta Memory Panel"
    }
  };

  function getLang() {
    try {
      var q = new URLSearchParams(location.search).get('lang');
      if (q === 'en' || q === 'zh') return q;
      var s = localStorage.getItem('fs_lang');
      if (s === 'en' || s === 'zh') return s;
      return (navigator.language || '').toLowerCase().indexOf('en') === 0 ? 'en' : 'zh';
    } catch (e) { return 'zh'; }
  }

  var LANG = getLang();
  var dict = DICT[LANG] || {};

  function t(s) {
    if (LANG === 'zh') return s;
    return dict[s] || s;
  }

  function apply() {
    if (LANG === 'zh') return;
    try {
      document.querySelectorAll('[data-i18n]').forEach(function (el) {
        var k = el.getAttribute('data-i18n'), v = dict[k];
        if (v && el.textContent !== v) el.textContent = v;
      });
      document.querySelectorAll('[data-i18n-ph]').forEach(function (el) {
        var k = el.getAttribute('data-i18n-ph'), v = dict[k];
        if (v) el.placeholder = v;
      });
      document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
        var k = el.getAttribute('data-i18n-title'), v = dict[k];
        if (v) el.title = v;
      });
    } catch (e) {}
  }

  window.__t = t;
  window.__LANG = LANG;
  window.__i18nApply = apply;
  window.toggleLang = function () {
    try { localStorage.setItem('fs_lang', LANG === 'en' ? 'zh' : 'en'); } catch (e) {}
    location.reload();
  };
  window.setLang = function (l) {
    if (l !== 'en' && l !== 'zh') return;
    try { localStorage.setItem('fs_lang', l); } catch (e) {}
    var url = new URL(location.href);
    url.searchParams.set('lang', l);
    location.href = url.href;
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply);
  else apply();
  // 动态 DOM 更新后自动重应用（去重防抖）
  var timer = null;
  if (window.MutationObserver) {
    new MutationObserver(function () {
      clearTimeout(timer);
      timer = setTimeout(apply, 200);
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
