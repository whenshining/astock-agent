/* A股智能选股助手 —— 前端逻辑（原生 JS，无外部依赖） */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

  const state = {
    sessions: [],
    sessionId: null,
    config: null,
    usage: null,
    strategies: [],
    streaming: false,
    scanTimer: null,
  };

  /* ------------------------------------------------------------ 工具函数 */

  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // 极简 markdown：表格、粗体、行内代码、列表、标题
  function renderMarkdown(text) {
    let html = escapeHtml(text);

    // Markdown 表格（模型很爱用）：连续的 | ... | 行，第二行是 |---|---| 分隔
    html = html.replace(/(?:^\|.*\|[ \t]*\n?)+/gm, (block) => {
      const rows = block.trim().split('\n').map((line) =>
        line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim())
      );
      // 至少要表头 + 分隔行，否则当普通文本
      if (rows.length < 2 || !rows[1].every((cell) => /^:?-{2,}:?$/.test(cell))) return block;
      const head = rows[0];
      const body = rows.slice(2);
      return '<table class="md-table"><thead><tr>' +
        head.map((cell) => '<th>' + cell + '</th>').join('') +
        '</tr></thead><tbody>' +
        body.map((row) => '<tr>' + row.map((cell) => '<td>' + cell + '</td>').join('') + '</tr>').join('') +
        '</tbody></table>\n';
    });

    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/^[-*] (.+)$/gm, '· $1');
    html = html.replace(/^#{1,6} (.+)$/gm, '<strong>$1</strong>');
    return html;
  }

  function fmtNum(value, digits) {
    if (value === null || value === undefined || value === '') return '—';
    const num = Number(value);
    if (!isFinite(num)) return '—';
    return num.toFixed(digits === undefined ? 2 : digits);
  }

  function fmtAmount(value) {
    if (value === null || value === undefined) return '—';
    const num = Number(value);
    if (!isFinite(num)) return '—';
    if (Math.abs(num) >= 1e8) return (num / 1e8).toFixed(2) + '亿';
    if (Math.abs(num) >= 1e4) return (num / 1e4).toFixed(0) + '万';
    return num.toFixed(0);
  }

  function fmtDate(value) {
    const s = String(value || '');
    if (s.length !== 8) return s || '—';
    return s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8);
  }

  function trendClass(value) {
    const num = Number(value);
    if (!isFinite(num) || num === 0) return 'flat';
    return num > 0 ? 'up' : 'down';
  }

  let toastTimer = null;
  function toast(message, isError) {
    const el = $('#toast');
    el.textContent = message;
    el.className = 'toast' + (isError ? ' err' : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 5200 : 2600);
  }

  async function api(path, options) {
    const opts = Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {});
    const res = await fetch(path, opts);
    let data;
    try {
      data = await res.json();
    } catch (err) {
      data = { ok: false, error: '服务器响应异常（HTTP ' + res.status + '）' };
    }
    if (data && data.ok === false && data.error) throw new Error(data.error);
    return data;
  }

  /* ------------------------------------------------------------ 会话管理 */

  async function loadSessions() {
    try {
      const data = await api('/api/sessions');
      state.sessions = data.sessions || [];
      renderSessions();
    } catch (err) {
      toast('加载会话失败：' + err.message, true);
    }
  }

  function renderSessions() {
    const list = $('#session-list');
    if (!state.sessions.length) {
      list.innerHTML = '<div class="empty-hint">还没有对话</div>';
      return;
    }
    list.innerHTML = state.sessions.map((s) => {
      const active = s.id === state.sessionId ? ' active' : '';
      return '<div class="session-item' + active + '" data-id="' + escapeHtml(s.id) + '">' +
        '<span class="title">' + escapeHtml(s.title || '新对话') + '</span>' +
        '<button class="del" title="删除">×</button></div>';
    }).join('');
  }

  async function createSession(title) {
    const data = await api('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: title || '新对话' }),
    });
    state.sessionId = data.session_id;
    await loadSessions();
    showWelcome(true);
    return state.sessionId;
  }

  async function switchSession(id) {
    if (state.streaming) { toast('正在对话中，请稍候再切换', true); return; }
    state.sessionId = id;
    renderSessions();
    showWelcome(false);
    try {
      const data = await api('/api/sessions/' + encodeURIComponent(id) + '/messages');
      renderHistory(data.messages || []);
    } catch (err) {
      toast('加载消息失败：' + err.message, true);
    }
  }

  async function deleteSession(id) {
    if (!confirm('确定删除这个对话吗？')) return;
    await api('/api/sessions/' + encodeURIComponent(id), { method: 'DELETE' });
    if (state.sessionId === id) {
      state.sessionId = null;
      Array.from(messagesEl().children).forEach((child) => {
        if (child.id !== 'welcome') child.remove();
      });
      showWelcome(true);
    }
    await loadSessions();
    if (!state.sessions.length) await createSession();
    else if (!state.sessionId) await switchSession(state.sessions[0].id);
  }

  /* ------------------------------------------------------------ 消息渲染 */

  function messagesEl() { return $('#messages'); }

  function showWelcome(show) {
    const welcome = $('#welcome');
    if (welcome) welcome.hidden = !show;
    if (show) {
      // 清掉除欢迎页之外的内容
      Array.from(messagesEl().children).forEach((child) => {
        if (child.id !== 'welcome') child.remove();
      });
    }
  }

  function scrollToBottom() {
    const el = messagesEl();
    el.scrollTop = el.scrollHeight;
  }

  function wrapBlock(node) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-wrap';
    wrap.appendChild(node);
    return wrap;
  }

  function addUserMessage(text) {
    showWelcome(false);
    const node = document.createElement('div');
    node.className = 'msg user';
    node.innerHTML = '<div class="msg-avatar">我</div>' +
      '<div class="msg-body"><div class="bubble">' + escapeHtml(text) + '</div></div>';
    messagesEl().appendChild(wrapBlock(node));
    scrollToBottom();
  }

  function addAssistantMessage() {
    const node = document.createElement('div');
    node.className = 'msg assistant';
    node.innerHTML = '<div class="msg-avatar">A</div>' +
      '<div class="msg-body"><div class="bubble"><span class="cursor"></span></div></div>';
    const bubble = node.querySelector('.bubble');
    messagesEl().appendChild(wrapBlock(node));
    scrollToBottom();
    let text = '';
    let thinking = false;
    return {
      setThinking(on) {
        thinking = on;
        if (!text) bubble.innerHTML = '<span class="thinking">正在思考…</span>';
        scrollToBottom();
      },
      append(delta) {
        text += delta;
        thinking = false;
        bubble.innerHTML = renderMarkdown(text) + '<span class="cursor"></span>';
        scrollToBottom();
      },
      finish() {
        const cursor = bubble.querySelector('.cursor');
        if (cursor) cursor.remove();
        if (!text.trim()) {
          // 空回复（只调用了工具）就不显示气泡
          node.remove();
        } else {
          bubble.innerHTML = renderMarkdown(text);
        }
      },
      getText() { return text; },
      getNode() { return node; },
    };
  }

  function addToolCard(name, label, args) {
    const node = document.createElement('div');
    node.className = 'tool-card';
    node.innerHTML =
      '<div class="tool-head">' +
        '<span class="tool-spinner"></span>' +
        '<span class="tool-icon">⚙</span>' +
        '<span class="tool-summary">' + escapeHtml(label || name) + '</span>' +
        '<span class="tool-meta"></span>' +
        '<span class="tool-arrow">▶</span>' +
      '</div>' +
      '<div class="tool-body" hidden></div>';
    const head = node.querySelector('.tool-head');
    const body = node.querySelector('.tool-body');
    head.addEventListener('click', () => {
      body.hidden = !body.hidden;
      node.classList.toggle('expanded', !body.hidden);
    });
    messagesEl().appendChild(wrapBlock(node));
    scrollToBottom();

    const card = {
      setResult(event) {
        const spinner = node.querySelector('.tool-spinner');
        if (spinner) spinner.remove();
        const icon = node.querySelector('.tool-icon');
        if (icon) icon.textContent = event.ok ? '✓' : '!';
        if (icon && !event.ok) icon.style.background = '#4a2029';
        node.querySelector('.tool-summary').textContent = event.summary || name;
        const meta = node.querySelector('.tool-meta');
        if (meta && event.elapsed_ms != null) meta.textContent = event.elapsed_ms + 'ms';

        if (!event.ok) {
          body.innerHTML = '<div class="table-note" style="color:#f6465d">' +
            escapeHtml(event.error || '执行失败') + '</div>';
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.knowledge && event.knowledge.length) {
          // 知识库检索命中：把出处和原文都摊开，方便用户核对引用是否属实
          body.innerHTML =
            '<div class="cond-list"><span class="cond-tag">检索：' +
              escapeHtml(event.query || '') + '</span></div>' +
            '<div class="kb-sources">' + event.knowledge.map(renderKbSource).join('') + '</div>';
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.name === 'search_knowledge_base' && event.ok) {
          body.innerHTML = '<div class="table-note">知识库里没有找到相关内容 —— ' +
            '助手会如实说明「没有相关记载」，不会硬凑建议。</div>';
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.backtest) {
          // 回测 / 样本外验证：把结论直接摊在对话里
          body.innerHTML = renderBacktestCard(event.backtest, event.mode);
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.strategy && event.stocks) {
          // run_strategy：策略标签 + 结果表格
          body.innerHTML =
            '<div class="strategy-chip"><span class="k">策略</span>' +
              escapeHtml(event.strategy.name) + '</div>' +
            '<div class="table-note" style="margin-bottom:8px">' +
              escapeHtml(event.strategy.summary || '') + '</div>' +
            (event.stocks.length
              ? renderStockTable(event.stocks, event)
              : '<div class="table-note">没有股票命中这个策略的条件。</div>');
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.strategy) {
          // save_strategy：保存/更新策略的回执
          body.innerHTML =
            '<div class="strategy-chip"><span class="k">' +
              (event.strategy.replaced ? '已更新' : '已保存') + '</span>' +
              escapeHtml(event.strategy.name) + '</div>' +
            '<div class="table-note">' + escapeHtml(event.strategy.summary || '') +
              '　可在左下角「自选策略」里查看或修改。</div>';
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.strategies) {
          // list_strategies：策略清单
          body.innerHTML = event.strategies.length
            ? '<div class="kb-sources">' + event.strategies.map((s) =>
                '<div class="kb-source"><div class="src-head"><span>' + escapeHtml(s.name) + '</span>' +
                '<span class="src-score">用过 ' + (s.uses || 0) + ' 次</span></div>' +
                '<div class="src-text">' + escapeHtml(s.summary) + '</div></div>').join('') + '</div>'
            : '<div class="table-note">还没有保存任何策略。</div>';
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.stocks && event.stocks.length) {
          body.innerHTML = renderStockTable(event.stocks, event);
          body.hidden = false;
          node.classList.add('expanded');
        } else if (event.detail) {
          body.innerHTML = '<pre style="font-size:11.5px;color:#9aa7b4;overflow:auto;max-height:260px">' +
            escapeHtml(JSON.stringify(event.detail, null, 2)) + '</pre>';
        }
      },
    };
    // 流式响应里只能通过 DOM 找到最近的卡片，因此把回填函数挂在节点上
    node.__setResult = (event) => card.setResult(event);
    return card;
  }

  // 对话里的回测卡片：结论放在最前面，数字放后面
  function renderBacktestCard(res, mode) {
    if (!res || !res.ok) {
      return '<div class="table-note" style="color:#f6465d">' +
        escapeHtml((res && res.error) || '没有拿到结果') + '</div>';
    }
    const strategy = res.strategy || {};
    const head = '<div class="strategy-chip"><span class="k">' +
      (mode === 'validate' ? '样本外验证' : '回测') + '</span>' +
      escapeHtml(strategy.name || '') + '　<span class="dim">' +
      res.start_date + '~' + res.end_date + '</span></div>';

    if (mode === 'validate') {
      const s = res.summary || {};
      const tone = s.level === 'good' ? 'good' : (s.level === 'bad' ? 'bad' : 'warn');
      const bm = res.benchmark;
      const folds = (res.folds || []).map((f) =>
        '<tr><td>第 ' + f.fold + ' 折</td>' +
        '<td>' + f.in_sample.start + '~' + f.in_sample.end + '</td>' +
        '<td>' + f.chosen_plan.hold_days + '天/止损' +
          Math.abs(f.chosen_plan.stop_loss || 0).toFixed(0) + '%</td>' +
        '<td class="' + (f.in_sample.return_pct >= 0 ? 'up' : 'down') + '">' +
          (f.in_sample.return_pct >= 0 ? '+' : '') + f.in_sample.return_pct.toFixed(2) + '%</td>' +
        '<td class="' + (f.out_sample.return_pct >= 0 ? 'up' : 'down') + '"><b>' +
          (f.out_sample.return_pct >= 0 ? '+' : '') + f.out_sample.return_pct.toFixed(2) + '%</b></td>' +
        '<td>' + f.out_sample.trade_count + '</td></tr>').join('');
      return head +
        '<div class="bt-verdict ' + tone + '"><div class="vl">样本外验证结论</div>' +
        '<div class="vt">' + escapeHtml(s.verdict || '') + '</div>' +
        '<div class="vd">样本内平均 <b>' + (s.in_sample_avg >= 0 ? '+' : '') +
          s.in_sample_avg.toFixed(2) + '%</b>　样本外平均 <b>' +
          (s.out_sample_avg >= 0 ? '+' : '') + s.out_sample_avg.toFixed(2) +
          '%</b>　样本外累计 <b>' + (s.out_sample_total >= 0 ? '+' : '') +
          s.out_sample_total.toFixed(2) + '%</b>' +
          (bm ? '　同期' + escapeHtml(bm.name) + ' <b>' + (bm.return_pct >= 0 ? '+' : '') +
            bm.return_pct.toFixed(2) + '%</b>' : '') +
          '</div></div>' +
        '<div class="stock-table-wrap"><table class="stock-table"><thead><tr>' +
        '<th>折</th><th>样本内区间</th><th>选出参数</th><th>样本内</th><th>样本外</th><th>笔数</th>' +
        '</tr></thead><tbody>' + folds + '</tbody></table></div>';
    }

    // 普通回测：可信度 + 基准对比放最前面
    const credHtml = renderCredibility(res.credibility);
    const bm = res.benchmark;
    const excess = res.excess_return;
    let benchHtml = '';
    if (bm) {
      const win = (excess || 0) >= 0;
      benchHtml = '<div class="bt-bench ' + (win ? 'win' : 'lose') + '">' +
        '策略 <b class="' + (res.total_return >= 0 ? 'up' : 'down') + '">' +
        (res.total_return >= 0 ? '+' : '') + res.total_return.toFixed(2) + '%</b>' +
        '　vs　' + escapeHtml(bm.name) + ' <b class="' + (bm.return_pct >= 0 ? 'up' : 'down') +
        '">' + (bm.return_pct >= 0 ? '+' : '') + bm.return_pct.toFixed(2) + '%</b>' +
        '　→ 超额 <b class="' + (win ? 'up' : 'down') + '">' + (win ? '+' : '') +
        (excess || 0).toFixed(2) + '%</b>' +
        (win ? '' : '<div class="w">⚠ 跑输基准——同期买指数比这个策略强。</div>') + '</div>';
    }
    const yearly = (res.yearly || []).map((y) =>
      '<tr><td>' + y.year + '</td><td class="' + (y.return_pct >= 0 ? 'up' : 'down') + '">' +
      (y.return_pct >= 0 ? '+' : '') + y.return_pct.toFixed(2) + '%</td>' +
      '<td class="down">' + y.max_drawdown.toFixed(2) + '%</td></tr>').join('');

    return head + credHtml + benchHtml +
      '<div class="bt-mini">' +
      '<span>总收益 <b class="' + (res.total_return >= 0 ? 'up' : 'down') + '">' +
        (res.total_return >= 0 ? '+' : '') + res.total_return.toFixed(2) + '%</b></span>' +
      '<span>最大回撤 <b class="down">' + res.max_drawdown.toFixed(2) + '%</b></span>' +
      '<span>胜率 <b>' + res.win_rate.toFixed(1) + '%</b></span>' +
      '<span>盈亏比 <b>' + res.profit_factor.toFixed(2) + '</b></span>' +
      '<span>交易 <b>' + res.trade_count + '</b> 笔</span>' +
      '</div>' +
      (yearly ? '<div class="bt-chart-title">分年度</div>' +
        '<div class="stock-table-wrap"><table class="stock-table"><thead><tr>' +
        '<th>年份</th><th>收益</th><th>年内回撤</th></tr></thead><tbody>' + yearly +
        '</tbody></table></div>' : '') +
      '<div class="table-note">单次回测只是历史表现，<b>不能证明策略有效</b>——' +
      '让助手做「样本外验证」，用没见过的数据检验才靠谱。</div>';
  }

  // 可信度卡片：等级 + 逐条清单。
  // 刻意不把分值放大——一个「72 分」只会给人虚假的确定感，
  // 用户真正需要知道的是「哪一项没过关」。
  function renderCredibility(c) {
    if (!c || !c.checks) return '';
    const tone = c.level === 'high' ? 'good' : (c.level === 'low' ? 'bad' : 'warn');
    const icon = { pass: '✓', warn: '!', fail: '✗', info: '·' };
    const items = c.checks.map((x) =>
      '<div class="cr-item ' + x.status + '">' +
      '<span class="ic">' + (icon[x.status] || '·') + '</span>' +
      '<span class="nm">' + escapeHtml(x.name) + '</span>' +
      '<span class="dt">' + escapeHtml(x.detail) + '</span></div>').join('');
    return '<div class="bt-cred ' + tone + '">' +
      '<div class="hd"><span class="lv">可信度 · ' + escapeHtml(c.label) + '</span>' +
      (c.capped ? '<span class="cap">已封顶</span>' : '') + '</div>' +
      '<div class="sm">' + escapeHtml(c.summary || '') + '</div>' +
      '<div class="list">' + items + '</div></div>';
  }

  function renderStockTable(stocks, event) {
    const conditions = (event.conditions || []).map(
      (c) => '<span class="cond-tag">' + escapeHtml(c) + '</span>'
    ).join('');

    const rows = stocks.map((s, index) => {
      const tags =
        (s.new_high60 ? '<span class="tag-new-high">新高</span>' : '') +
        (s.ma_bull ? '<span class="tag-bull">多头</span>' : '');
      return '<tr data-code="' + escapeHtml(s.code) + '">' +
        '<td class="rank">' + (index + 1) + '</td>' +
        '<td><span class="code">' + escapeHtml(s.code) + '</span>' +
          (s.name ? ' <span class="name">' + escapeHtml(s.name) + '</span>' : '') + tags + '</td>' +
        '<td>' + fmtNum(s.close) + '</td>' +
        '<td class="' + trendClass(s.chg) + '">' + (s.chg > 0 ? '+' : '') + fmtNum(s.chg) + '%</td>' +
        '<td>' + fmtNum(s.vol_ratio) + '</td>' +
        '<td>' + fmtNum(s.amplitude) + '%</td>' +
        '<td>' + (s.consec_up || 0) + '</td>' +
        '<td>' + fmtAmount(s.amount) + '</td>' +
      '</tr>';
    }).join('');

    const note = '<div class="table-note">共命中 ' + (event.total != null ? event.total : stocks.length) +
      ' 只' + (event.trade_date ? '，数据截至 ' + fmtDate(event.trade_date) : '') +
      '；点击任意行查看日线走势。仅供研究，非投资建议。</div>';

    return (conditions ? '<div class="cond-list">' + conditions + '</div>' : '') +
      '<div class="stock-table-wrap"><table class="stock-table"><thead><tr>' +
      '<th>#</th><th>股票</th><th>收盘</th><th>涨跌幅</th><th>量比</th><th>振幅</th><th>连涨</th><th>成交额</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>' + note;
  }

  function renderHistory(messages) {
    const container = messagesEl();
    // 只清掉消息，保留欢迎页节点（它是常驻的兄弟节点）
    Array.from(container.children).forEach((child) => {
      if (child.id !== 'welcome') child.remove();
    });
    showWelcome(messages.length === 0);
    let bubble = null;

    messages.forEach((msg) => {
      if (msg.role === 'user') {
        addUserMessage(msg.content || '');
        bubble = null;
      } else if (msg.role === 'assistant') {
        if (msg.content && msg.content.trim()) {
          bubble = addAssistantMessage();
          bubble.append(msg.content);
          bubble.finish();
        }
        bubble = null;
      } else if (msg.role === 'tool' && msg.meta) {
        const meta = msg.meta;
        const card = addToolCard(meta.name || 'tool', meta.summary, null);
        card.setResult({
          ok: meta.ok,
          name: meta.name,
          summary: meta.summary,
          stocks: meta.stocks,
          total: meta.total,
          conditions: meta.conditions,
          trade_date: meta.trade_date,
          knowledge: meta.knowledge,
          query: meta.query,
          strategy: meta.strategy,
          strategies: meta.strategies,
          error: meta.error,
        });
      }
    });
    scrollToBottom();
  }

  /* ------------------------------------------------------------ 发送消息 */

  async function sendMessage(text) {
    if (state.streaming) return;
    text = (text || '').trim();
    if (!text) return;
    if (!state.sessionId) await createSession();

    addUserMessage(text);
    state.streaming = true;
    updateSendButton();

    let bubble = null;
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.sessionId, message: text }),
      });

      if (!res.ok || !res.body) {
        throw new Error('请求失败（HTTP ' + res.status + '）');
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sep;
        while ((sep = buffer.indexOf('\n\n')) >= 0) {
          const chunk = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          for (const line of chunk.split('\n')) {
            if (!line.startsWith('data:')) continue;
            const payload = line.slice(5).trim();
            if (!payload || payload === '[DONE]') continue;
            let event;
            try { event = JSON.parse(payload); } catch (e) { continue; }
            bubble = handleEvent(event, bubble);
          }
        }
      }
    } catch (err) {
      toast(err.message || '对话失败', true);
      if (bubble) bubble.finish();
    } finally {
      if (bubble) bubble.finish();
      state.streaming = false;
      updateSendButton();
      loadSessions();
    }
  }

  function handleEvent(event, bubble) {
    switch (event.type) {
      case 'text':
        if (!bubble) bubble = addAssistantMessage();
        bubble.append(event.delta || '');
        return bubble;

      case 'reasoning':
        // 思考模式：模型先推理再作答，先给个「正在思考…」的反馈
        if (!bubble) bubble = addAssistantMessage();
        bubble.setThinking(true);
        return bubble;

      case 'tool_start':
        addToolCard(event.name, event.label, event.args);
        return null;   // 工具之后的新文本另起气泡

      case 'tool_result': {
        const cards = messagesEl().querySelectorAll('.tool-card');
        const last = cards[cards.length - 1];
        if (last && last.__setResult) last.__setResult(event);
        else if (last) {
          // 从 DOM 反查（刷新历史时不经此路径）
          const spinner = last.querySelector('.tool-spinner');
          if (spinner) spinner.remove();
        }
        return null;
      }

      case 'error':
        toast(event.message || '出错了', true);
        if (!bubble) {
          bubble = addAssistantMessage();
          bubble.append('⚠️ ' + (event.message || '出错了'));
        }
        return bubble;

      case 'done':
        // 本轮结束后顺手更新左下角的用量卡片（done 事件里带了最新汇总）
        if (event.summary && state.usage) {
          state.usage.summary = event.summary;
          renderUsageCard(state.usage);
        }
        return bubble;

      default:
        return bubble;
    }
  }

  function updateSendButton() {
    $('#btn-send').disabled = state.streaming || !$('#input').value.trim();
  }

  /* ------------------------------------------------------------ 配置 */

  async function loadConfig() {
    try {
      const data = await api('/api/config');
      state.config = data.config;
      // 模型下拉由后端驱动的（模型会下线换代，前端不写死过期选项）
      const sel = $('#cfg-model');
      const choices = data.config.model_choices || [];
      sel.innerHTML = choices.map((c) =>
        '<option value="' + escapeHtml(c.value) + '">' + escapeHtml(c.label) + '</option>'
      ).join('');
      sel.value = data.config.model || (choices[0] ? choices[0].value : '');
      if (!sel.value && choices.length) sel.value = choices[0].value;

      // 模型详情由后端给（版本/上下文/能力），前端不写死——模型换代时只改后端
      const details = data.config.model_details || {};
      const renderModelDetail = () => {
        const d = details[sel.value];
        const box = $('#model-detail');
        if (!d) { box.innerHTML = ''; return; }
        box.innerHTML =
          '<span class="tag">' + escapeHtml(d.version || sel.value) + '</span>' +
          '<span>上下文 ' + escapeHtml(d.context || '—') + '</span>' +
          '<span>最大输出 ' + escapeHtml(d.max_output || '—') + '</span>' +
          '<span class="' + (d.vision ? 'yes' : 'no') + '">' +
            (d.vision ? '✓ 支持图片' : '✕ 不支持图片') + '</span>' +
          '<div class="n">' + escapeHtml(d.note || '') + '</div>';
      };
      sel.onchange = renderModelDetail;
      renderModelDetail();

      const hint = $('#model-hint');
      if (hint) {
        hint.innerHTML = 'deepseek-flash 即 <b>V4.1-Flash</b>（2026-09-10 发布）。' +
          '旧的 deepseek-chat / deepseek-reasoner / deepseek-v4-flash 已退役，' +
          '服务端虽会转发但建议用新名字。';
      }

      const effSel = $('#cfg-thinking-effort');
      effSel.innerHTML = (data.config.thinking_efforts || []).map((e) =>
        '<option value="' + escapeHtml(e.value) + '">' + escapeHtml(e.label) + '</option>'
      ).join('');
      effSel.value = data.config.thinking_effort || 'high';

      $('#cfg-thinking').checked = data.config.thinking !== false;
      // 没配过就显示自动探测到的路径，用户一眼能看到程序实际在用哪个目录
      $('#cfg-vipdoc').value = data.config.vipdoc_configured || data.config.vipdoc_path || '';
      const vipdocHint = $('#vipdoc-hint');
      if (data.config.vipdoc_autodetected) {
        vipdocHint.innerHTML = '已自动找到：<code>' + escapeHtml(data.config.vipdoc_path) +
          '</code>（不用改；要换别的目录直接填这里）';
      } else if (data.config.vipdoc_path) {
        vipdocHint.textContent = '日线数据目录（指向通达信安装目录下的 vipdoc）';
      } else {
        vipdocHint.innerHTML = '<span style="color:#d29922">没找到通达信数据目录，请手动填写安装目录下的 vipdoc 路径</span>';
      }
      $('#cfg-memory').checked = data.config.memory_enabled !== false;
      $('#cfg-data-dir').textContent = data.config.data_dir || '';
      // 显示版本，免得分不清自己跑的是哪个版本
      const ver = $('#cfg-version');
      if (ver) {
        ver.textContent = (data.config.version_label || '') +
          (data.config.version ? '（' + data.config.version + '）' : '');
      }
      if (data.config.api_key_set) {
        $('#cfg-api-key').placeholder = data.config.api_key_masked + '（已保存，留空则不修改）';
        $('#api-key-hint').textContent = '已配置 Key。留空保存不会覆盖原有 Key。';
      }
    } catch (err) {
      toast('读取配置失败：' + err.message, true);
    }
  }

  async function saveConfig() {
    const body = {
      vipdoc_path: $('#cfg-vipdoc').value.trim(),
      model: $('#cfg-model').value,
      thinking: $('#cfg-thinking').checked,
      thinking_effort: $('#cfg-thinking-effort').value,
      memory_enabled: $('#cfg-memory').checked,
    };
    const key = $('#cfg-api-key').value.trim();
    if (key && key.indexOf('***') < 0) body.deepseek_api_key = key;
    try {
      const data = await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
      state.config = data.config;
      $('#cfg-api-key').value = '';
      toast('设置已保存');
      loadConfig();
      loadStatus();
    } catch (err) {
      toast('保存失败：' + err.message, true);
    }
  }

  async function verifyKey() {
    const el = $('#verify-result');
    el.className = 'verify-result';
    el.textContent = '测试中…';
    try {
      const data = await api('/api/config/verify', { method: 'POST' });
      el.className = 'verify-result ok';
      el.textContent = '连接正常（' + (data.model || '') + '）';
    } catch (err) {
      el.className = 'verify-result err';
      el.textContent = err.message;
    }
  }

  /* ------------------------------------------------------------ 用量与余额 */

  function currencySymbol(code) {
    if (code === 'CNY') return '¥';
    if (code === 'USD') return '$';
    return code ? code + ' ' : '';
  }

  function fmtCost(costUsd, rate) {
    const cny = Number(costUsd || 0) * (Number(rate) || 7.2);
    if (!cny) return '¥0';
    if (cny >= 1) return '¥' + cny.toFixed(2);
    if (cny >= 0.01) return '¥' + cny.toFixed(3);
    return '¥' + cny.toFixed(4);
  }

  function fmtTokens(n) {
    const num = Number(n || 0);
    if (num >= 1e6) return (num / 1e6).toFixed(2) + 'M';
    if (num >= 1e3) return (num / 1e3).toFixed(1) + 'k';
    return String(num);
  }

  async function loadUsage(refresh) {
    try {
      const data = await api('/api/usage' + (refresh ? '?refresh=1' : ''));
      state.usage = data;
      renderUsageCard(data);
      renderUsageModal(data);
    } catch (err) {
      const el = $('#usage-balance');
      el.className = 'usage-value err';
      el.textContent = '查询失败';
      el.title = err.message;
    }
  }

  function renderUsageCard(data) {
    const bal = data.balance || {};
    const balanceEl = $('#usage-balance');
    if (bal.ok) {
      const amount = Number(bal.total_balance);
      balanceEl.className = 'usage-value' + (amount <= 0 ? ' warn' : '');
      balanceEl.textContent = currencySymbol(bal.currency) + amount.toFixed(2);
      balanceEl.title = '总余额 ' + bal.total_balance + '（充值 ' + bal.topped_up_balance +
        ' + 赠送 ' + bal.granted_balance + '）';
    } else {
      balanceEl.className = 'usage-value err';
      balanceEl.textContent = (bal.error || '').indexOf('未配置') >= 0 ? '未配置 Key' : '余额不可用';
      balanceEl.title = bal.error || '';
    }

    const today = (data.summary || {}).today || {};
    const todayEl = $('#usage-today');
    todayEl.textContent = today.calls ? fmtCost(today.cost_usd, data.rate) : '¥0';
    todayEl.title = (today.calls || 0) + ' 次调用 · ' + fmtTokens(today.tokens) + ' tokens';
  }

  function renderUsageModal(data) {
    const bal = data.balance || {};
    const box = $('#usage-balance-box');
    if (bal.ok) {
      const amount = Number(bal.total_balance);
      box.innerHTML =
        '<div class="balance-amount' + (amount <= 0 ? ' warn' : '') + '">' +
          currencySymbol(bal.currency) + amount.toFixed(2) + '</div>' +
        '<div class="balance-sub">充值余额 ' + currencySymbol(bal.currency) + bal.topped_up_balance +
          ' &nbsp;·&nbsp; 赠送余额 ' + currencySymbol(bal.currency) + bal.granted_balance +
          (bal.is_available ? '' : '<br><span style="color:#d29922">⚠ 账户余额不足，API 调用可能失败</span>') +
        '</div>';
    } else {
      box.innerHTML = '<div class="balance-amount warn" style="font-size:14px">' +
        escapeHtml(bal.error || '余额查询失败') + '</div>' +
        '<div class="balance-sub">费用统计仍然可用（基于本地记账）</div>';
    }

    const summary = data.summary || {};
    const today = summary.today || {};
    const overall = summary.total || {};
    const cells = [
      ['今日花费', fmtCost(today.cost_usd, data.rate), (today.calls || 0) + ' 次模型调用'],
      ['今日 tokens', fmtTokens(today.tokens),
        '输入 ' + fmtTokens(today.prompt_tokens) + ' · 输出 ' + fmtTokens(today.completion_tokens)],
      ['缓存命中率', (today.cache_hit_rate || 0) + '%',
        '命中 ' + fmtTokens(today.cache_hit_tokens) + ' tokens（命中单价低 50 倍）'],
      ['累计花费', fmtCost(overall.cost_usd, data.rate),
        (overall.calls || 0) + ' 次调用 · ' + fmtTokens(overall.tokens) + ' tokens'],
    ];
    $('#usage-grid').innerHTML = cells.map((cell) =>
      '<div class="usage-cell"><div class="k">' + cell[0] + '</div><div class="v">' + cell[1] +
      '</div><div class="s">' + cell[2] + '</div></div>'
    ).join('');

    const models = summary.by_model || [];
    $('#usage-models').innerHTML = models.length
      ? '<table class="stock-table"><thead><tr><th>模型</th><th>调用</th><th>tokens</th><th>今日花费</th></tr></thead>' +
        '<tbody>' + models.map((m) =>
          '<tr><td>' + escapeHtml(m.model) + '</td><td>' + m.calls + '</td><td>' +
          fmtTokens(m.tokens) + '</td><td>' + fmtCost(m.cost_usd, data.rate) + '</td></tr>'
        ).join('') + '</tbody></table>'
      : '';

    const pricing = summary.pricing || {};
    $('#usage-note').innerHTML =
      '费用按 DeepSeek 官方价目在本地估算，当前为' +
      (pricing.is_peak_now ? '高峰时段（价格翻倍）' : '低谷时段') +
      '；高峰时段为 UTC 周一至周五 01:00-04:00 与 06:00-10:00。' +
      '费用以美元计价，按 1 : ' + data.rate + ' 折算人民币展示；余额为官方实时数据。';
  }

  /* ------------------------------------------------------------ 回测 */

  const btState = {
    strategy: null, spec: null, exits: [], timer: null,
    mode: 'sweep',
    // 用户从结果页点过「返回修改参数」之后，重开弹窗就不要再自动跳回结果页，
    // 否则关了重开又被弹回结果，等于出不来
    dismissed: false,
  };

  const SWEEP_FIELDS = [
    { key: 'hold_days', label: '最多持有（天）', values: [3, 5, 10, 20], default: [3, 5, 10] },
    { key: 'stop_loss', label: '止损（%）', values: [4, 5, 8, 10, 15], default: [5, 8, 10] },
    { key: 'take_profit', label: '止盈（%）', values: [8, 10, 15, 20, 30], default: [10, 15, 20] },
  ];

  async function openBacktest(strategy) {
    btState.strategy = strategy;
    btState.exits = ((strategy.plan && strategy.plan.exit_conditions) || [])
      .map((c) => Object.assign({}, c));

    if (!btState.spec) {
      try {
        const meta = await api('/api/backtest/meta');
        btState.spec = meta;
        $('#bt-range').innerHTML = meta.ranges.map((r) =>
          '<option value="' + r.value + '"' + (r.value === meta.default_range ? ' selected' : '') +
          '>' + escapeHtml(r.label) + '</option>').join('');
        $('#bt-exit-new').innerHTML = meta.exit_condition_types.map((t) =>
          '<option value="' + t.type + '">' + escapeHtml(t.label) + '</option>').join('');
        renderExitTypeOptions();
        $('#bt-costs').innerHTML =
          '已按真实交易约束模拟：佣金 <code>' + meta.costs.commission +
          '</code>、卖出印花税 <code>' + meta.costs.stamp_tax +
          '</code>；<b>信号次日开盘买入</b>（不用当日收盘价，避免偷看未来）、' +
          '<b>一字涨停买不进</b>、<b>一字跌停卖不出</b>、<b>T+1</b>、停牌不交易。';
      } catch (err) {
        toast('读取回测配置失败：' + err.message, true);
        return;
      }
    }

    $('#bt-title').textContent = '回测：' + strategy.name;
    $('#bt-desc').innerHTML = '买入条件：' + escapeHtml(strategy.summary);
    $('#bt-hint').textContent = '';
    renderPlanForm(strategy.plan);
    renderExits();
    renderExitTypeOptions();
    renderExitParams();
    renderSweepFields();
    showBtPanels('config');
    // 回测面板在 DOM 里排在策略面板前面，不关掉策略面板会被它盖住
    $('#strategy-modal').hidden = true;
    $('#backtest-modal').hidden = false;

    // 如果这个策略刚跑过回测、结果还在，就直接展示（方便回看）
    // 想改参数点「返回修改参数」即可
    if (btState.dismissed) return;
    try {
      const state = await api('/api/backtest/status');
      if (state.result && !state.running && !state.error &&
          String(state.strategy_id || '') === String(strategy.id)) {
        // 按上次跑的类型渲染（回测 / 参数扫描 / 样本外验证，结果结构不一样）
        if (state.kind === 'walkforward') renderWalkForwardResult(state.result);
        else if (state.kind === 'sweep') renderSweepResult(state.result);
        else renderBacktestResult(state.result);
      }
    } catch (err) { /* 忽略，正常走配置流程 */ }
  }

  function showBtPanels(which) {
    $('#bt-config').hidden = which !== 'config';
    $('#bt-grid-config').hidden = which !== 'grid';
    $('#bt-progress').hidden = which !== 'progress';
    $('#bt-result').hidden = which !== 'result';
    $('#bt-sweep-result').hidden = which !== 'sweep';
    $('#bt-wf-result').hidden = which !== 'walkforward';
  }

  // 参数扫描和样本外验证共用「选参数」界面，只是后面跑的东西不同
  function openGrid(mode) {
    btState.mode = mode;
    $('#btn-bt-sweep-run').textContent = mode === 'walkforward'
      ? '开始样本外验证' : '开始扫描对比';
    showBtPanels('grid');
  }

  // 每个结果页统一带上退回入口——不然跑完一次就只能关掉重开
  function btToolbar(mode) {
    const extra = (mode === 'sweep' || mode === 'walkforward')
      ? '<button class="btn btn-ghost" data-bt-back="grid" data-mode="' + mode + '">' +
        (mode === 'walkforward' ? '换个参数再验证' : '换个参数再扫描') + '</button>'
      : '';
    return '<div class="bt-toolbar">' +
      '<button class="btn btn-ghost" data-bt-back="config">← 返回修改参数</button>' +
      extra +
      '<button class="btn btn-ghost" data-bt-back="rerun" data-mode="' + mode +
      '">重新跑一次</button>' +
      // 打开回测时策略面板被藏起来了，没这个按钮就只能关掉弹窗回到聊天
      '<button class="btn btn-ghost bt-right" data-bt-back="list">返回策略列表</button>' +
      '</div>';
  }

  function renderPlanForm(plan) {
    const spec = (btState.spec && btState.spec.plan_spec) || [];
    const values = plan || {};
    let html = '';
    for (const item of spec) {
      let value = values[item.key];
      if (item.key === 'stop_loss' && value != null) value = Math.abs(value);  // 内部负数、界面填正数
      if (value === null || value === undefined) value = '';
      const unit = item.unit ? ' <span class="unit">' + escapeHtml(item.unit) + '</span>' : '';
      html += '<div class="sp-field"><label for="btp-' + item.key + '">' + escapeHtml(item.label) +
        unit + '</label><input type="number" step="any" id="btp-' + item.key + '" value="' +
        escapeHtml(String(value)) + '" placeholder="不限"></div>';
    }
    $('#bt-plan').innerHTML = html;
  }

  function collectPlan() {
    const read = (key) => {
      const el = $('#btp-' + key);
      if (!el) return null;
      const raw = String(el.value).trim();
      return raw === '' ? null : raw;
    };
    return {
      hold_days: read('hold_days') || 5,
      stop_loss: read('stop_loss'),
      take_profit: read('take_profit'),
      max_positions: read('max_positions') || 5,
      position_pct: read('position_pct') || 20,
      exit_conditions: btState.exits,
    };
  }

  function exitTypeSpec(type) {
    const types = (btState.spec && btState.spec.exit_condition_types) || [];
    return types.find((t) => t.type === type);
  }

  // 预设条件 + 最后的「自定义表达式」。自定义能让用户写出预设覆盖不了的条件，
  // 比如「收盘 < MA10 且 量比 < 0.8」。
  function renderExitTypeOptions() {
    const types = (btState.spec && btState.spec.exit_condition_types) || [];
    $('#bt-exit-new').innerHTML =
      types.map((t) => '<option value="' + escapeHtml(t.type) + '">' +
        escapeHtml(t.label) + '</option>').join('') +
      '<option value="expr">✎ 自定义表达式…</option>';
  }

  // 根据选中的条件类型，就地渲染它需要的参数输入（不再用浏览器那种原生弹窗）
  function renderExitParams() {
    const type = $('#bt-exit-new').value;
    const box = $('#bt-exit-params');
    const help = $('#bt-expr-help');

    if (type === 'expr') {
      box.innerHTML = '<input type="text" id="bt-expr-input" ' +
        'placeholder="例如：收盘 &lt; MA10 且 量比 &lt; 0.8">';
      help.hidden = false;
      renderExprHelp();
      const input = $('#bt-expr-input');
      input.addEventListener('input', () => validateExprInput());
      // 输入框里按回车直接添加
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); addExitCondition(); }
      });
      input.focus();
      return;
    }

    help.hidden = true;
    const spec = exitTypeSpec(type);
    if (!spec) { box.innerHTML = ''; return; }
    if (spec.value_type === 'ma') {
      box.innerHTML = '<select id="bt-exit-value">' +
        (spec.choices || []).map((c) => '<option value="' + escapeHtml(c.value) + '">' +
          escapeHtml(c.label) + '</option>').join('') + '</select>';
    } else if (spec.value_type === 'none') {
      box.innerHTML = '';
    } else {
      box.innerHTML = '<input type="number" step="any" id="bt-exit-value" style="width:120px" ' +
        'placeholder="' + escapeHtml(spec.placeholder || '') + '">' +
        (spec.unit ? ' <span class="unit">' + escapeHtml(spec.unit) + '</span>' : '');
    }
  }

  function renderExprHelp() {
    const spec = btState.spec || {};
    const fields = (spec.expr_fields || []).map((f) =>
      '<div><b>' + escapeHtml(f.name) + '</b>　' + escapeHtml(f.desc) + '</div>').join('');
    const examples = (spec.expr_examples || []).map((e) =>
      '<code class="expr-eg">' + escapeHtml(e) + '</code>').join('');
    $('#bt-expr-help').innerHTML =
      '<div class="h">可用字段</div><div class="fields">' + fields + '</div>' +
      '<div class="h">连接与比较</div>' +
      '<div class="tip">用 <b>且</b> / <b>或</b> 连接多个条件；比较符：' +
      '<code>&lt;</code> <code>&lt;=</code> <code>&gt;</code> <code>&gt;=</code> <code>=</code>；' +
      '右边可以填数字，也可以填另一个字段（如 MA10）</div>' +
      '<div class="h">示例（点一下直接填入）</div><div class="egs">' + examples + '</div>' +
      '<div class="msg" id="bt-expr-msg"></div>';
    // 点示例直接填进输入框
    $('#bt-expr-help').addEventListener('click', (ev) => {
      const eg = ev.target.closest('.expr-eg');
      if (!eg) return;
      const input = $('#bt-expr-input');
      if (input) { input.value = eg.textContent; input.focus(); validateExprInput(); }
    });
  }

  // 前端先校验一遍，错在哪马上说，不用等提交
  function checkExpr(text) {
    const names = new Set(((btState.spec && btState.spec.expr_field_names) || [])
      .map((n) => String(n).toLowerCase()));
    const raw = String(text || '').trim();
    if (!raw) return '表达式不能为空';
    const orParts = raw.split(/\s*(?:或|或者|or|\|\|)\s*/i);
    for (const orPart of orParts) {
      if (!orPart.trim()) return '「或」两边都要有条件';
      for (const atom of orPart.split(/\s*(?:且|并且|而且|and|&&)\s*/i)) {
        const piece = atom.trim();
        const m = piece.match(/^([A-Za-z0-9\u4e00-\u9fa5_]+)\s*(<=|>=|==|!=|<|>|=)\s*([A-Za-z0-9\u4e00-\u9fa5_]+|-?\d+(?:\.\d+)?)$/);
        if (!m) return '看不懂「' + piece + '」，写法是「字段 比较符 数值或字段」';
        if (!names.has(m[1].toLowerCase())) return '没有「' + m[1] + '」这个字段';
        const right = m[3];
        if (!/^-?\d+(?:\.\d+)?$/.test(right) && !names.has(right.toLowerCase())) {
          return '「' + right + '」既不是数字，也不是可用字段';
        }
      }
    }
    return null;
  }

  function validateExprInput() {
    const msg = $('#bt-expr-msg');
    if (!msg) return null;
    const err = checkExpr(($('#bt-expr-input') || {}).value);
    msg.textContent = err || '';
    msg.className = 'msg' + (err ? ' bad' : '');
    return err;
  }

  function exitLabel(rule) {
    // 自定义表达式：显示成一个标签 + 用户自己写的条件
    if (rule.type === 'expr') {
      return '<span class="expr-tag">自定义</span>' + escapeHtml(rule.expr || '');
    }
    const types = (btState.spec && btState.spec.exit_condition_types) || [];
    const spec = types.find((t) => t.type === rule.type);
    const label = spec ? spec.label : rule.type;
    if (rule.type === 'ma_break') {
      const choices = (spec && spec.choices) || [];
      const picked = choices.find((c) => c.value === rule.ma);
      return label + ' ' + escapeHtml(picked ? picked.label : (rule.ma || 'MA10'));
    }
    if (rule.type === 'ma_dead') return label;
    return label + ' <span class="val">' + escapeHtml(String(rule.value)) +
      escapeHtml((spec && spec.unit) || '') + '</span>';
  }

  function renderExits() {
    if (!btState.exits.length) {
      $('#bt-exits').innerHTML = '<div class="kb-hint">还没有条件卖出。只用上面的止盈止损也可以回测。</div>';
      return;
    }
    $('#bt-exits').innerHTML = btState.exits.map((rule, index) =>
      '<div class="bt-exit-item"><span class="lbl">' + exitLabel(rule) + '</span>' +
      '<button data-idx="' + index + '" title="移除">×</button></div>').join('');
  }

  function addExitCondition() {
    const type = $('#bt-exit-new').value;
    if (btState.exits.length >= 6) { toast('最多 6 个条件卖出', true); return; }

    // 自定义表达式
    if (type === 'expr') {
      const input = $('#bt-expr-input');
      const text = input ? input.value.trim() : '';
      const err = checkExpr(text);
      if (err) {
        validateExprInput();
        toast(err, true);
        if (input) input.focus();
        return;
      }
      btState.exits.push({ type: 'expr', expr: text });
      renderExits();
      if (input) input.value = '';
      validateExprInput();
      return;
    }

    const spec = exitTypeSpec(type);
    if (!spec) return;

    if (spec.value_type === 'ma') {
      btState.exits.push({ type: type, ma: $('#bt-exit-value').value });
    } else if (spec.value_type === 'none') {
      btState.exits.push({ type: type });
    } else {
      // 数值就地输入，不再弹浏览器原生对话框
      const el = $('#bt-exit-value');
      const raw = el ? String(el.value).trim() : '';
      if (raw === '') { toast('请填写数值', true); if (el) el.focus(); return; }
      const value = parseFloat(raw);
      if (!isFinite(value)) { toast('请输入数字', true); if (el) el.focus(); return; }
      btState.exits.push({ type: type, value: value });
    }
    renderExits();
    renderExitParams();          // 清空输入，方便接着加下一个
  }

  function renderSweepFields() {
    $('#bt-sweep-fields').innerHTML = SWEEP_FIELDS.map((field) =>
      '<div class="bt-sweep-field"><div class="h">' + escapeHtml(field.label) + '</div>' +
      '<div class="opts">' + field.values.map((v) =>
        '<label><input type="checkbox" data-field="' + field.key + '" value="' + v + '"' +
        (field.default.indexOf(v) >= 0 ? ' checked' : '') + '>' + v + '</label>').join('') +
      '</div></div>').join('');
  }

  function collectGrid() {
    const grid = {};
    document.querySelectorAll('#bt-sweep-fields input[type=checkbox]:checked').forEach((el) => {
      const key = el.dataset.field;
      if (!grid[key]) grid[key] = [];
      grid[key].push(parseFloat(el.value));
    });
    return grid;
  }

  async function startBacktest(kind) {
    if (!btState.strategy) return;
    // 重新跑了一次，结果页恢复正常自动展示
    btState.dismissed = false;
    const body = {
      strategy_id: btState.strategy.id,
      plan: collectPlan(),
      range: $('#bt-range').value,
      capital: parseFloat($('#bt-capital').value) || 100000,
    };
    if (kind === 'sweep' || kind === 'walkforward') {
      const grid = collectGrid();
      if (!Object.keys(grid).length) { toast('至少勾选一个参数取值', true); return; }
      const combos = Object.keys(grid).reduce((n, k) => n * grid[k].length, 1);
      if (combos > 60) { toast('组合数 ' + combos + ' 太多，请减少勾选（上限 60）', true); return; }
      body.grid = grid;
      if (kind === 'walkforward') body.folds = 3;   // 切 3 折：3 段样本内 + 3 段样本外
    }
    const endpoint = kind === 'sweep' ? '/api/backtest/sweep'
      : (kind === 'walkforward' ? '/api/backtest/walkforward' : '/api/backtest/run');
    try {
      const res = await api(endpoint, { method: 'POST', body: JSON.stringify(body) });
      if (!res.started) { toast(res.reason || '启动失败', true); return; }
      showBtPanels('progress');
      $('#bt-fill').style.width = '0%';
      $('#bt-progress-text').textContent = '正在准备…';
      pollBacktest(kind);
    } catch (err) {
      toast('启动失败：' + err.message, true);
    }
  }

  function pollBacktest(kind) {
    if (btState.timer) clearInterval(btState.timer);
    btState.timer = setInterval(async () => {
      let state;
      try {
        state = await api('/api/backtest/status');
      } catch (err) {
        clearInterval(btState.timer);
        btState.timer = null;
        toast('读取回测状态失败：' + err.message, true);
        return;
      }
      const pct = state.total ? Math.round(state.done / state.total * 100) : 0;
      $('#bt-fill').style.width = pct + '%';
      $('#bt-progress-text').textContent = (state.stage || '处理中') +
        (state.total ? ' ' + state.done + '/' + state.total : ' ' + state.done) +
        '　已用 ' + (state.elapsed || 0) + ' 秒';

      if (state.running) return;
      clearInterval(btState.timer);
      btState.timer = null;

      if (state.error) {
        showBtPanels('config');
        toast('回测失败：' + state.error, true);
        return;
      }
      if (kind === 'sweep') renderSweepResult(state.result);
      else if (kind === 'walkforward') renderWalkForwardResult(state.result);
      else renderBacktestResult(state.result);
    }, 1500);
  }

  function renderWalkForwardResult(res) {
    showBtPanels('walkforward');
    if (!res || !res.ok) {
      $('#bt-wf-result').innerHTML = btToolbar('walkforward') + '<div class="kb-empty">' +
        escapeHtml((res && res.error) || '没有拿到验证结果') + '</div>';
      return;
    }
    const s = res.summary;
    const tone = s.level === 'good' ? 'good' : (s.level === 'bad' ? 'bad' : 'warn');

    const folds = (res.folds || []).map((f) => {
      const p = f.chosen_plan;
      return '<tr><td>第 ' + f.fold + ' 折</td>' +
        '<td>' + f.in_sample.start + '~' + f.in_sample.end + '</td>' +
        '<td>' + p.hold_days + '天 / 止损' + Math.abs(p.stop_loss || 0).toFixed(0) + '%</td>' +
        '<td class="' + (f.in_sample.return_pct >= 0 ? 'up' : 'down') + '">' +
          (f.in_sample.return_pct >= 0 ? '+' : '') + f.in_sample.return_pct.toFixed(2) + '%</td>' +
        '<td>' + f.out_sample.start + '~' + f.out_sample.end + '</td>' +
        '<td class="' + (f.out_sample.return_pct >= 0 ? 'up' : 'down') + '"><b>' +
          (f.out_sample.return_pct >= 0 ? '+' : '') + f.out_sample.return_pct.toFixed(2) + '%</b></td>' +
        '<td class="down">' + f.out_sample.max_drawdown.toFixed(2) + '%</td>' +
        '<td>' + f.out_sample.trade_count + '</td>' +
        '<td>' + f.out_sample.win_rate.toFixed(1) + '%</td></tr>';
    }).join('');

    const bm = res.benchmark;
    const bmLine = bm ? '<div class="bt-bench"><span class="t">同期基准</span> ' +
      escapeHtml(bm.name) + ' 买入持有 <b class="' +
      (bm.return_pct >= 0 ? 'up' : 'down') + '">' +
      (bm.return_pct >= 0 ? '+' : '') + bm.return_pct.toFixed(2) + '%</b>' +
      '　样本外累计 <b class="' + (s.out_sample_total >= 0 ? 'up' : 'down') + '">' +
      (s.out_sample_total >= 0 ? '+' : '') + s.out_sample_total.toFixed(2) + '%</b>' +
      '　→ 超额 <b class="' + (s.out_sample_total - bm.return_pct >= 0 ? 'up' : 'down') + '">' +
      (s.out_sample_total - bm.return_pct >= 0 ? '+' : '') +
      (s.out_sample_total - bm.return_pct).toFixed(2) + '%</b></div>' : '';

    $('#bt-wf-result').innerHTML = btToolbar('walkforward') +
      '<div class="bt-verdict ' + tone + '"><div class="vl">样本外验证结论</div>' +
      '<div class="vt">' + escapeHtml(s.verdict) + '</div>' +
      '<div class="vd">样本内平均 <b>' + (s.in_sample_avg >= 0 ? '+' : '') + s.in_sample_avg.toFixed(2) +
      '%</b>　样本外平均 <b>' + (s.out_sample_avg >= 0 ? '+' : '') + s.out_sample_avg.toFixed(2) +
      '%</b>　样本外累计 <b>' + (s.out_sample_total >= 0 ? '+' : '') + s.out_sample_total.toFixed(2) +
      '%</b>（10 万 → ' + Math.round(s.final_equity).toLocaleString() + ' 元）　' +
      '各折选出的参数组合：' + s.distinct_plans + ' 种</div></div>' + bmLine +
      '<div class="bt-chart-title">逐折明细</div>' +
      '<div class="stock-table-wrap"><table class="stock-table"><thead><tr>' +
      '<th>折</th><th>样本内区间</th><th>选出的最优参数</th><th>样本内收益</th>' +
      '<th>样本外区间</th><th>样本外收益</th><th>回撤</th><th>笔数</th><th>胜率</th>' +
      '</tr></thead><tbody>' + folds + '</tbody></table></div>' +
      '<div class="table-note">样本外 = 调参时<b>没看过</b>的数据。这是判断策略真假的关键：' +
      '样本内漂亮、样本外崩掉，说明参数是"记住"了历史而不是"理解"了规律。' +
      (s.distinct_plans > 1
        ? ' 本次各折选出的最优参数都不一样，说明根本不存在稳定的最优解。' : '') + '</div>';
  }

  function renderBacktestResult(res) {
    if (!res) { showBtPanels('config'); toast('没有拿到回测结果', true); return; }
    showBtPanels('result');
    $('#bt-toolbar-single').innerHTML = btToolbar('single');
    // 可信度放最前面——它决定后面这些数字值不值得看
    $('#bt-credibility').innerHTML = renderCredibility(res.credibility);
    // 只在自选股范围内回测时，必须把"事后选股"这个前提说清楚
    const uniWarn = res.universe_warning
      ? '<div class="bt-warn">⚠ ' + escapeHtml(res.universe_warning) + '</div>' : '';
    const up = res.total_return >= 0;
    const cells = [
      ['总收益', (up ? '+' : '') + res.total_return.toFixed(2) + '%', up ? 'up' : 'down',
        res.initial_capital.toLocaleString() + ' → ' + res.final_equity.toLocaleString() + ' 元'],
      ['最大回撤', res.max_drawdown.toFixed(2) + '%', 'down',
        '回撤越浅越拿得住，比收益更该看'],
      ['胜率', res.win_rate.toFixed(1) + '%', '',
        res.win_count + ' 盈 / ' + res.loss_count + ' 亏，共 ' + res.trade_count + ' 笔'],
      ['盈亏比', res.profit_factor.toFixed(2), '',
        '平均盈 ' + Math.round(res.avg_win) + ' / 亏 ' + Math.round(Math.abs(res.avg_loss))],
      ['平均持仓', res.avg_hold_days.toFixed(1) + ' 天', '',
        '计划最多 ' + ((btState.strategy && btState.strategy.plan &&
                        btState.strategy.plan.hold_days) || '—') + ' 天'],
      ['回测区间', String(res.start_date) + '~' + String(res.end_date).slice(4), '',
        res.trading_days + ' 个交易日'],
      ['涨停没买进', ((res.skipped && res.skipped.limit_up) || 0) + ' 次', '',
        '一字涨停按真实规则剔除，不虚增收益'],
      ['扫描范围', (res.scanned || 0) + ' 只', '',
        '产生信号 ' + (res.signal_days || 0) + ' 个交易日'],
    ];
    $('#bt-metrics').innerHTML = cells.map((c) =>
      '<div class="bt-metric"><div class="k">' + c[0] + '</div><div class="v ' + c[2] + '">' +
      c[1] + '</div><div class="s">' + escapeHtml(c[3]) + '</div></div>').join('');

    // 一笔都没成交时，把原因说清楚（可能只是条件没触发，也可能是买不起一手）
    const zeroNote = res.zero_trade_reason
      ? '<div class="bt-warn">⚠ 一笔都没成交：' + escapeHtml(res.zero_trade_reason) + '</div>'
      : '';

    // 基准对比：没有对照的收益率没有意义
    const bm = res.benchmark;
    let benchHtml = '';
    if (bm) {
      const excess = res.total_return - bm.return_pct;
      const win = excess >= 0;
      benchHtml =
        '<div class="bt-bench ' + (win ? 'win' : 'lose') + '">' +
        '<span class="t">相对基准</span> 策略 <b class="' + (res.total_return >= 0 ? 'up' : 'down') +
        '">' + (res.total_return >= 0 ? '+' : '') + res.total_return.toFixed(2) + '%</b>' +
        '　vs　' + escapeHtml(bm.name) + ' 买入持有 <b class="' +
        (bm.return_pct >= 0 ? 'up' : 'down') + '">' + (bm.return_pct >= 0 ? '+' : '') +
        bm.return_pct.toFixed(2) + '%</b>' +
        '　→ 超额收益 <b class="' + (win ? 'up' : 'down') + '">' +
        (win ? '+' : '') + excess.toFixed(2) + '%</b>' +
        (win ? '' : '<div class="w">⚠ 跑输基准。这段时间就算买入指数不动，也比这个策略强——' +
          '说明它没有产生任何超额价值。</div>') + '</div>';
    }

    $('#bt-benchmark').innerHTML = zeroNote + uniWarn + benchHtml;

    // 分年度：看策略是不是只在某一段行情里有效
    if (res.yearly && res.yearly.length) {
      $('#bt-yearly').innerHTML = '<div class="bt-chart-title">分年度表现</div>' +
        '<div class="stock-table-wrap"><table class="stock-table"><thead><tr>' +
        '<th>年份</th><th>当年收益</th><th>年内最大回撤</th><th>交易日</th><th>年末净值</th>' +
        '</tr></thead><tbody>' + res.yearly.map((y) =>
          '<tr><td>' + y.year + '</td>' +
          '<td class="' + (y.return_pct >= 0 ? 'up' : 'down') + '">' +
            (y.return_pct >= 0 ? '+' : '') + y.return_pct.toFixed(2) + '%</td>' +
          '<td class="down">' + y.max_drawdown.toFixed(2) + '%</td>' +
          '<td>' + y.trading_days + '</td>' +
          '<td>' + Math.round(y.end_equity).toLocaleString() + '</td></tr>').join('') +
        '</tbody></table></div>';
    } else {
      $('#bt-yearly').innerHTML = '';
    }

    drawEquityCurve($('#bt-equity'), res.equity_curve, res.initial_capital);

    const trades = (res.trades || []).slice().reverse();
    $('#bt-trades-title').textContent = '逐笔交易（共 ' + trades.length + ' 笔，最近的在前）';
    $('#bt-trades').innerHTML = trades.length
      ? '<div class="stock-table-wrap"><table class="stock-table"><thead><tr>' +
        '<th>买入日</th><th>代码</th><th>买入价</th><th>卖出日</th><th>卖出价</th>' +
        '<th>盈亏%</th><th>盈亏(元)</th><th>持有</th><th>卖出原因</th></tr></thead><tbody>' +
        trades.slice(0, 200).map((t) =>
          '<tr data-code="' + escapeHtml(t.code) + '"><td>' + t.buy_date + '</td>' +
          '<td>' + escapeHtml(t.code) + '</td><td>' + t.buy_price + '</td>' +
          '<td>' + t.sell_date + '</td><td>' + t.sell_price + '</td>' +
          '<td class="' + (t.pnl_pct >= 0 ? 'up' : 'down') + '">' +
            (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct + '%</td>' +
          '<td class="' + (t.pnl >= 0 ? 'up' : 'down') + '">' + Math.round(t.pnl) + '</td>' +
          '<td>' + t.hold_days + '天</td>' +
          '<td style="text-align:left">' + escapeHtml(t.reason) + '</td></tr>'
        ).join('') + '</tbody></table></div>' +
        (trades.length > 200 ? '<div class="table-note">只显示最近 200 笔</div>' : '')
      : '<div class="kb-empty">这段时间没有产生任何交易。</div>';
  }

  function renderSweepResult(res) {
    if (!res || !res.ok || !res.results || !res.results.length) {
      showBtPanels('sweep');
      $('#bt-sweep-result').innerHTML = btToolbar('sweep') +
        '<div class="kb-empty">' + escapeHtml((res && res.error) || '没有扫描结果') + '</div>';
      return;
    }
    showBtPanels('sweep');
    const returns = res.results.map((r) => r.total_return);
    const spread = Math.max.apply(null, returns) - Math.min.apply(null, returns);
    const best = res.best;

    let warn = '';
    if (Math.max.apply(null, returns) < 0) {
      warn = '<div class="bt-warn">⚠ 所有参数组合都亏损 → <b>这个买入条件本身无效</b>，' +
        '换卖点救不回来，要改的是买入条件。</div>';
    } else if (spread > 30) {
      warn = '<div class="bt-warn">⚠ 收益跨度高达 ' + spread.toFixed(1) +
        ' 个百分点：相邻参数结果差这么多，通常说明<b>这是参数碰巧凑出来的，不是真优势</b>。' +
        '建议换一段时间再扫一遍——如果最优参数跟着变，基本可以确认是噪声。</div>';
    } else if (spread < 15) {
      warn = '<div class="bt-warn" style="color:#2ebd85;background:#12231b;border-color:#1f4a37">' +
        '✓ 各参数结果比较接近（跨度 ' + spread.toFixed(1) +
        ' 个百分点），说明这个策略对参数不敏感，相对可信。</div>';
    }

    $('#bt-sweep-result').innerHTML = btToolbar('sweep') +
      '<div class="bt-best"><div class="t">最佳组合（按总收益排序）</div><div class="d">持有 ' +
        best.plan.hold_days + ' 天 · 止损 ' + Math.abs(best.plan.stop_loss || 0).toFixed(0) +
        '% · 收益 ' + best.total_return.toFixed(2) + '% · 回撤 ' + best.max_drawdown.toFixed(2) +
        '% · ' + best.trade_count + ' 笔</div></div>' + warn +
      '<div class="kb-stats">共 ' + res.combos + ' 组参数 · 收益区间 ' +
        Math.min.apply(null, returns).toFixed(2) + '% ~ ' +
        Math.max.apply(null, returns).toFixed(2) + '%</div>' +
      '<div class="stock-table-wrap"><table class="stock-table"><thead><tr>' +
      '<th>持有</th><th>止损</th><th>总收益</th><th>最大回撤</th><th>交易数</th>' +
      '<th>胜率</th><th>盈亏比</th><th>平均持仓</th></tr></thead><tbody>' +
      res.results.map((r) =>
        '<tr><td>' + r.plan.hold_days + '</td>' +
        '<td>' + Math.abs(r.plan.stop_loss || 0).toFixed(0) + '%</td>' +
        '<td class="' + (r.total_return >= 0 ? 'up' : 'down') + '">' +
          (r.total_return >= 0 ? '+' : '') + r.total_return.toFixed(2) + '%</td>' +
        '<td class="down">' + r.max_drawdown.toFixed(2) + '%</td>' +
        '<td>' + r.trade_count + '</td><td>' + r.win_rate.toFixed(1) + '%</td>' +
        '<td>' + r.profit_factor.toFixed(2) + '</td>' +
        '<td>' + r.avg_hold_days.toFixed(1) + '</td></tr>'
      ).join('') + '</tbody></table></div>' +
      '<div class="table-note">参数扫描是在同一段行情上反复调参，' +
      '<b>收益最高的那组不代表将来能赚</b>——重点是看不同参数下结果稳不稳定。</div>';
  }

  function drawEquityCurve(canvas, points, initial) {
    if (!canvas || !points || points.length < 2) return;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 900;
    const height = 230;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.height = height + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const padL = 8, padR = 66, padT = 14, padB = 24;
    const chartW = width - padL - padR;
    const chartH = height - padT - padB;

    const values = points.map((p) => p.equity);
    let lo = Math.min.apply(null, values.concat([initial]));
    let hi = Math.max.apply(null, values.concat([initial]));
    const span = (hi - lo) || 1;
    lo -= span * 0.08;
    hi += span * 0.08;

    const xAt = (i) => padL + chartW * (i / (points.length - 1));
    const yAt = (v) => padT + chartH - ((v - lo) / (hi - lo)) * chartH;

    ctx.strokeStyle = '#171e27';
    ctx.fillStyle = '#5c6874';
    ctx.font = '10.5px monospace';
    ctx.lineWidth = 1;
    for (let g = 0; g <= 4; g++) {
      const y = padT + (chartH / 4) * g;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + chartW, y); ctx.stroke();
      const value = hi - ((hi - lo) / 4) * g;
      ctx.fillText(Math.round(value).toLocaleString(), padL + chartW + 6, y + 3.5);
    }

    // 成本线（虚线）
    ctx.strokeStyle = '#3a4553';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, yAt(initial));
    ctx.lineTo(padL + chartW, yAt(initial));
    ctx.stroke();
    ctx.setLineDash([]);

    // 净值曲线：赚了红、亏了绿（A 股习惯）
    const finalValue = values[values.length - 1];
    ctx.strokeStyle = finalValue >= initial ? '#f6465d' : '#2ebd85';
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = xAt(i), y = yAt(p.equity);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    ctx.fillStyle = '#5c6874';
    ctx.font = '10.5px monospace';
    ctx.fillText(String(points[0].date).slice(4), padL, height - 6);
    ctx.fillText(String(points[points.length - 1].date).slice(4), padL + chartW - 26, height - 6);
  }

  /* ------------------------------------------------------------ 自选股 */

  async function loadWatchlist() {
    const body = $('#watchlist-body');
    body.innerHTML = '<div class="kb-stats">读取中…</div>';
    try {
      const data = await api('/api/watchlist');
      renderWatchlist(data);
      $('#watchlist-count').textContent = data.available ? (data.count || 0) : 0;
      // 每次都是直接重读通达信的 .blk 文件，所以这里显示的就是真实读取时间
      const stamp = $('#watchlist-updated');
      if (stamp) {
        stamp.textContent = data.available
          ? '已读取最新的自选股 · ' + new Date().toLocaleTimeString('zh-CN')
          : '';
      }
    } catch (err) {
      body.innerHTML = '<div class="kb-stats" style="color:#f6465d">' + escapeHtml(err.message) + '</div>';
    }
  }

  function renderWatchlist(data) {
    const body = $('#watchlist-body');
    if (!data.available) {
      body.innerHTML = '<div class="kb-empty">' + escapeHtml(data.error || '没读到自选股') +
        '<br><br>自选股存在通达信的 <code>T0002\\blocknew\\ZXG.blk</code>。' +
        '请确认通达信已安装，且你至少添加过一只自选股（指数不算）。</div>';
      return;
    }

    const stocks = data.stocks || [];
    const missing = data.missing || [];
    const freshness = data.days_ago == null ? ''
      : (data.days_ago === 0 ? '（今天更新过）' : '（该文件 ' + data.days_ago + ' 天前更新过）');
    const head = '<div class="kb-stats">你的自选股共 <b>' + (data.count || 0) + '</b> 只' +
      freshness +
      (missing.length ? '；其中 ' + missing.length + ' 只在本地行情里没有数据（' +
        escapeHtml(missing.slice(0, 6).join('、')) + '）' : '') +
      '</div>';

    body.innerHTML = head + (stocks.length
      ? renderStockTable(stocks.slice(0, 100), { total: stocks.length, conditions: [] })
      : '<div class="kb-empty">自选股里没有可显示的股票。</div>');
  }

  /* ------------------------------------------------------------ 自选策略 */

  let strategySpec = null;
  let editingStrategyId = null;

  async function loadStrategies(query) {
    try {
      const data = await api('/api/strategies' + (query ? '?q=' + encodeURIComponent(query) : ''));
      strategySpec = data.spec || strategySpec;
      renderStrategies(data);
      return data;
    } catch (err) {
      toast('读取策略失败：' + err.message, true);
      return null;
    }
  }

  function renderStrategies(data) {
    const stats = data.stats || {};
    $('#strategy-count').textContent = stats.count || 0;
    $('#strategy-stats').innerHTML = stats.count
      ? '共 <b>' + stats.count + '</b> 个策略 · 累计执行 <b>' + (stats.uses || 0) + '</b> 次'
      : '';

    const items = data.strategies || [];
    state.strategies = items;
    $('#strategy-list').innerHTML = items.length
      ? items.map((item) =>
          '<div class="strategy-item" data-id="' + item.id + '">' +
            '<div class="info">' +
              '<div class="s-name">' + escapeHtml(item.name) + '</div>' +
              '<div class="s-summary">' + escapeHtml(item.summary) + '</div>' +
              (item.description ? '<div class="s-desc">' + escapeHtml(item.description) + '</div>' : '') +
            '</div>' +
            '<div class="s-actions">' +
              '<button data-act="backtest">回测</button>' +
              '<button data-act="run">试跑</button>' +
              '<button data-act="edit">编辑</button>' +
              '<button class="del" data-act="del">删除</button>' +
            '</div>' +
          '</div>'
        ).join('')
      : '<div class="kb-empty">还没有保存任何策略。<br>' +
        '点「新建策略」，或者直接对助手说「把涨幅大于2%、量比大于2 存成策略」。</div>';
  }

  function buildStrategyForm(values) {
    const spec = strategySpec || {};
    const container = $('#strategy-params');
    let html = '';
    let lastGroup = null;

    for (const p of (spec.params || [])) {
      if (p.group !== lastGroup) {
        html += '<div class="sp-group">' + escapeHtml(p.group || '其他') + '</div>';
        lastGroup = p.group;
      }
      const val = values ? values[p.key] : undefined;
      const unit = p.unit ? ' <span class="unit">' + escapeHtml(p.unit) + '</span>' : '';

      if (p.type === 'bool') {
        html += '<div class="sp-field sp-check">' +
          '<input type="checkbox" id="sp-' + p.key + '"' + (val ? ' checked' : '') + '>' +
          '<label for="sp-' + p.key + '">' + escapeHtml(p.label) + '</label></div>';
      } else if (p.type === 'boards') {
        html += '<div class="sp-field boards"><label>' + escapeHtml(p.label) + '</label><div class="sp-boards">' +
          (spec.board_options || []).map((b) =>
            '<label><input type="checkbox" data-board="' + b.value + '"' +
            (Array.isArray(val) && val.indexOf(b.value) >= 0 ? ' checked' : '') + '>' +
            escapeHtml(b.label) + '</label>').join('') +
          '</div></div>';
      } else if (p.type === 'sort' || p.type === 'order') {
        const options = p.type === 'sort'
          ? (spec.sort_options || [])
          : [{ value: 'desc', label: '降序' }, { value: 'asc', label: '升序' }];
        html += '<div class="sp-field"><label>' + escapeHtml(p.label) + unit + '</label>' +
          '<select id="sp-' + p.key + '"><option value="">（不设置）</option>' +
          options.map((o) => '<option value="' + o.value + '"' +
            (val === o.value ? ' selected' : '') + '>' + escapeHtml(o.label) + '</option>').join('') +
          '</select></div>';
      } else {
        let shown = '';
        if (val !== undefined && val !== null && val !== '') {
          if (p.type === 'amount' && p.scale) shown = val / p.scale;
          else if (p.type === 'drawdown') shown = Math.abs(val);
          else shown = val;
        }
        html += '<div class="sp-field"><label for="sp-' + p.key + '">' + escapeHtml(p.label) + unit + '</label>' +
          '<input type="number" step="any" id="sp-' + p.key + '" value="' + escapeHtml(String(shown)) +
          '" placeholder="不限"></div>';
      }
    }
    container.innerHTML = html;
  }

  function collectStrategyForm() {
    const params = {};
    for (const p of ((strategySpec || {}).params || [])) {
      if (p.type === 'bool') {
        const el = $('#sp-' + p.key);
        if (el && el.checked) params[p.key] = true;
      } else if (p.type === 'boards') {
        const picked = Array.from(document.querySelectorAll('#strategy-params [data-board]'))
          .filter((el) => el.checked).map((el) => el.dataset.board);
        if (picked.length) params[p.key] = picked;
      } else {
        const el = $('#sp-' + p.key);
        const raw = el ? String(el.value).trim() : '';
        if (raw !== '') params[p.key] = raw;   // 类型与单位换算交给服务端
      }
    }
    return params;
  }

  function openStrategyForm(strategy) {
    editingStrategyId = strategy ? strategy.id : null;
    $('#strategy-form').hidden = false;
    $('#strategy-form-title').textContent = strategy ? ('编辑策略：' + strategy.name) : '新建策略';
    $('#strategy-name').value = strategy ? strategy.name : '';
    $('#strategy-desc').value = strategy ? (strategy.description || '') : '';
    $('#strategy-form-msg').textContent = '';
    buildStrategyForm(strategy ? strategy.params : null);
    $('#strategy-form').scrollIntoView({ block: 'nearest' });
  }

  function closeStrategyForm() {
    editingStrategyId = null;
    $('#strategy-form').hidden = true;
  }

  async function saveStrategyForm() {
    const msg = $('#strategy-form-msg');
    const name = $('#strategy-name').value.trim();
    if (!name) {
      msg.className = 'verify-result err';
      msg.textContent = '请填写策略名称';
      return;
    }
    const params = collectStrategyForm();
    if (!Object.keys(params).length) {
      msg.className = 'verify-result err';
      msg.textContent = '至少要设置一个筛选条件';
      return;
    }
    msg.className = 'verify-result';
    msg.textContent = '保存中…';
    try {
      const url = editingStrategyId ? '/api/strategies/' + editingStrategyId : '/api/strategies';
      const data = await api(url, {
        method: 'POST',
        body: JSON.stringify({ name, description: $('#strategy-desc').value.trim(), params }),
      });
      strategySpec = data.spec || strategySpec;
      renderStrategies(data);
      closeStrategyForm();
      toast(data.saved && data.saved.replaced ? '策略已更新' : '策略已保存');
    } catch (err) {
      msg.className = 'verify-result err';
      msg.textContent = err.message;
    }
  }

  async function deleteStrategy(id, name) {
    if (!confirm('确定删除策略「' + name + '」吗？')) return;
    try {
      const data = await api('/api/strategies/' + id, { method: 'DELETE' });
      strategySpec = data.spec || strategySpec;
      renderStrategies(data);
      toast('已删除');
    } catch (err) {
      toast('删除失败：' + err.message, true);
    }
  }

  async function runStrategy(id, name) {
    const box = $('#strategy-run-result');
    box.innerHTML = '<div class="kb-stats" style="margin-top:14px">正在执行「' + escapeHtml(name) + '」…</div>';
    try {
      const data = await api('/api/strategies/' + id + '/run', { method: 'POST', body: '{}' });
      const head = '<div class="kb-stats" style="margin-top:14px">' +
        '<span class="strategy-chip"><span class="k">策略</span>' + escapeHtml(name) +
        '</span> 命中 <b>' + data.total + '</b> 只，展示前 ' + data.stocks.length + ' 只</div>';
      box.innerHTML = data.stocks.length
        ? head + renderStockTable(data.stocks, {
            total: data.total, trade_date: data.trade_date, conditions: [],
          })
        : head + '<div class="kb-stats">没有股票命中这些条件。</div>';
    } catch (err) {
      box.innerHTML = '<div class="kb-stats" style="margin-top:14px;color:#f6465d">' +
        escapeHtml(err.message) + '</div>';
    }
  }

  /* ------------------------------------------------------------ 知识库 */

  function renderKbSource(item) {
    return '<div class="kb-source">' +
      '<div class="src-head"><span>' + escapeHtml(item.citation || '') + '</span>' +
      '<span class="src-score">相关度 ' + (item.score != null ? item.score : '—') + '</span></div>' +
      '<div class="src-text">' + escapeHtml(item.text || '') + '</div>' +
    '</div>';
  }

  async function loadKb() {
    try {
      const data = await api('/api/kb');
      renderKb(data);
    } catch (err) {
      /* 知识库读取失败不影响主流程 */
    }
  }

  function renderKb(data) {
    const stats = data.stats || {};
    $('#kb-count').textContent = stats.documents || 0;
    const chars = stats.chars || 0;
    const sizeText = chars >= 10000
      ? Math.round(chars / 1000) + 'k 字'
      : chars.toLocaleString() + ' 字';
    $('#kb-stats').innerHTML = stats.documents
      ? '共 <b>' + stats.documents + '</b> 篇文档 · <b>' + stats.chunks +
        '</b> 个可检索片段 · <b>' + sizeText + '</b>'
      : '';

    const docs = data.documents || [];
    $('#kb-list').innerHTML = docs.length
      ? docs.map((doc) =>
          '<div class="kb-item">' +
            '<span class="doc-name">' + escapeHtml(doc.name) + '</span>' +
            '<span class="doc-meta">' + doc.chunk_count + ' 段 · ' + (doc.chars || 0) + ' 字</span>' +
            '<button class="del" data-id="' + doc.id + '" title="删除">×</button>' +
          '</div>'
        ).join('')
      : '<div class="kb-empty">还没有导入任何文档。<br>知识库为空时，助手不会给出任何建议性内容。</div>';
  }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || '');
        const comma = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(new Error('读取文件失败'));
      reader.readAsDataURL(file);
    });
  }

  async function importKbFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    let done = 0;
    const failures = [];
    for (const file of list) {
      try {
        const content_b64 = await readFileAsBase64(file);
        await api('/api/kb/import', {
          method: 'POST',
          body: JSON.stringify({ name: file.name, content_b64 }),
        });
        done += 1;
      } catch (err) {
        failures.push(file.name + '（' + err.message + '）');
      }
    }
    await loadKb();
    if (failures.length) {
      toast('导入成功 ' + done + ' 个；失败 ' + failures.length + ' 个：' + failures[0], true);
    } else {
      toast('已导入 ' + done + ' 个文档');
    }
  }

  async function kbSearch() {
    const query = $('#kb-query').value.trim();
    const box = $('#kb-search-result');
    if (!query) return;
    box.innerHTML = '<div class="kb-stats" style="margin-top:10px">检索中…</div>';
    try {
      const data = await api('/api/kb/search', {
        method: 'POST',
        body: JSON.stringify({ query, top_k: 5 }),
      });
      box.innerHTML = data.count
        ? '<div class="kb-stats" style="margin-top:10px">命中 ' + data.count + ' 段：</div>' +
          '<div class="kb-sources">' + data.results.map(renderKbSource).join('') + '</div>'
        : '<div class="kb-stats" style="margin-top:10px">没有命中任何内容 —— 这种情况下助手会直说' +
          '「知识库里没有相关记载」，而不是硬凑。</div>';
    } catch (err) {
      box.innerHTML = '<div class="kb-stats" style="margin-top:10px;color:#f6465d">' +
        escapeHtml(err.message) + '</div>';
    }
  }

  /* ------------------------------------------------------------ 记忆 */

  async function loadMemories() {
    try {
      const data = await api('/api/memories');
      renderMemories(data.memories || []);
      $('#memory-count').textContent = (data.memories || []).length;
    } catch (err) {
      toast('加载记忆失败：' + err.message, true);
    }
  }

  function renderMemories(memories) {
    const list = $('#memory-list');
    if (!memories.length) {
      list.innerHTML = '<div class="memory-empty">还没有长期记忆。聊得多了会自动积累，也可以手动添加。</div>';
      return;
    }
    const labels = { preference: '偏好', strategy: '策略', profile: '背景', note: '备注' };
    list.innerHTML = memories.map((m) =>
      '<div class="memory-item">' +
        '<span class="kind">' + escapeHtml(labels[m.kind] || '记忆') + '</span>' +
        '<span class="content">' + escapeHtml(m.content) + '</span>' +
        '<button class="del" data-id="' + m.id + '" title="删除">×</button>' +
      '</div>'
    ).join('');
  }

  /* ------------------------------------------------------------ 数据状态 */

  async function loadStatus() {
    try {
      const data = await api('/api/cache/status');
      const dot = $('#status-dot');
      const text = $('#status-text');
      const cache = data.cache || {};
      // 优先级：路径配错 > 上次扫描失败 > 正常运行 > 还没建缓存
      if (data.vipdoc_ok === false) {
        dot.className = 'dot warn';
        text.textContent = '找不到通达信数据目录：' + (data.vipdoc_path || '未设置') +
          '　—— 请点右上角「设置」修改路径';
      } else if (data.error) {
        dot.className = 'dot warn';
        text.textContent = '数据更新失败：' + data.error;
      } else if (cache.ready) {
        dot.className = 'dot ok';
        text.textContent = '数据截至 ' + fmtDate(cache.trade_date) + ' · ' + cache.count + ' 只';
      } else {
        dot.className = 'dot warn';
        text.textContent = '尚未建立行情缓存，请点「刷新数据」';
      }
      renderOverview(data);
      if (data.running) startScanPolling();
    } catch (err) {
      $('#status-text').textContent = '无法连接本地服务';
      $('#status-dot').className = 'dot warn';
    }
    // 顶栏右侧显示市场涨跌概览（与左侧的数据日期互补，避免重复）
    try {
      renderOverview(await api('/api/overview'));
    } catch (err) {
      /* 未建缓存时忽略 */
    }
  }

  function renderOverview(overview) {
    const el = $('#overview');
    if (!overview || !overview.ready) { el.innerHTML = ''; return; }
    el.innerHTML =
      '<span class="u">涨 <b>' + overview.up + '</b></span>' +
      '<span class="d">跌 <b>' + overview.down + '</b></span>' +
      '<span>涨停 <b class="u">' + overview.limit_up + '</b></span>' +
      '<span>跌停 <b class="d">' + overview.limit_down + '</b></span>';
  }

  async function refreshData() {
    try {
      await api('/api/cache/refresh', { method: 'POST', body: JSON.stringify({ force: true }) });
      startScanPolling();
    } catch (err) {
      toast('刷新失败：' + err.message, true);
    }
  }

  function startScanPolling() {
    const bar = $('#scan-bar');
    bar.hidden = false;
    if (state.scanTimer) clearInterval(state.scanTimer);
    state.scanTimer = setInterval(async () => {
      try {
        const data = await api('/api/cache/status');
        const total = data.total || 0;
        const done = data.done || 0;
        const pct = total ? Math.round(done / total * 100) : 0;
        $('#scan-fill').style.width = pct + '%';
        $('#scan-text').textContent = data.running
          ? '正在扫描本地日线数据… ' + done + '/' + total + '（' + pct + '%）'
          : '扫描完成';
        if (!data.running) {
          clearInterval(state.scanTimer);
          state.scanTimer = null;
          setTimeout(() => { $('#scan-bar').hidden = true; }, 1600);
          if (data.error) toast('扫描失败：' + data.error, true);
          else if (data.result && data.result.ok) {
            toast('数据已更新：' + fmtDate(data.result.trade_date) + '，共 ' + data.result.scanned + ' 只，用时 ' + data.result.elapsed + 's');
          }
          loadStatus();
        }
      } catch (err) {
        clearInterval(state.scanTimer);
        state.scanTimer = null;
        $('#scan-bar').hidden = true;
      }
    }, 700);
  }

  /* ------------------------------------------------------------ K线图 */

  async function showKline(code) {
    const modal = $('#kline-modal');
    modal.hidden = false;
    $('#kline-title').textContent = code + ' 日线走势';
    const canvas = $('#kline-canvas');
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#9aa7b4';
    ctx.font = '13px sans-serif';
    ctx.fillText('加载中…', 20, 30);

    try {
      const data = await api('/api/kline?code=' + encodeURIComponent(code) + '&days=120');
      if (!data.bars || !data.bars.length) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#9aa7b4';
        ctx.fillText('没有找到该股票的日线数据', 20, 30);
        return;
      }
      drawKline(canvas, data.bars);
    } catch (err) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#f6465d';
      ctx.fillText('加载失败：' + err.message, 20, 30);
    }
  }

  function drawKline(canvas, bars) {
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = canvas.clientWidth || 900;
    const cssHeight = 460;
    canvas.width = cssWidth * dpr;
    canvas.height = cssHeight * dpr;
    canvas.style.height = cssHeight + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    const padL = 8, padR = 58, padTop = 12;
    const mainH = 300, gap = 22, volH = 92;
    const chartW = cssWidth - padL - padR;

    const closes = bars.map((b) => b.close);
    const ma = (n) => closes.map((_, i) => {
      if (i < n - 1) return null;
      let sum = 0;
      for (let k = i - n + 1; k <= i; k++) sum += closes[k];
      return sum / n;
    });
    const ma5 = ma(5), ma10 = ma(10), ma20 = ma(20);

    let high = -Infinity, low = Infinity, maxVol = 0;
    bars.forEach((b, i) => {
      high = Math.max(high, b.high);
      low = Math.min(low, b.low);
      maxVol = Math.max(maxVol, b.volume);
      [ma5, ma10, ma20].forEach((series) => {
        if (series[i] != null) { high = Math.max(high, series[i]); low = Math.min(low, series[i]); }
      });
    });
    const range = (high - low) || 1;
    const pad = range * 0.06;
    high += pad; low -= pad;

    const step = chartW / bars.length;
    const candleW = Math.max(1.6, Math.min(9, step * 0.66));
    const yPrice = (v) => padTop + mainH - ((v - low) / (high - low)) * mainH;
    const xAt = (i) => padL + step * i + step / 2;

    // 背景网格
    ctx.strokeStyle = '#171e27';
    ctx.fillStyle = '#5c6874';
    ctx.font = '10.5px monospace';
    ctx.lineWidth = 1;
    for (let g = 0; g <= 4; g++) {
      const y = padTop + (mainH / 4) * g;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + chartW, y);
      ctx.stroke();
      const price = high - ((high - low) / 4) * g;
      ctx.fillText(price.toFixed(2), padL + chartW + 6, y + 3.5);
    }

    // 蜡烛
    bars.forEach((b, i) => {
      const x = xAt(i);
      const up = b.close >= b.open;
      const color = up ? '#f6465d' : '#2ebd85';
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x, yPrice(b.high));
      ctx.lineTo(x, yPrice(b.low));
      ctx.stroke();
      const yo = yPrice(b.open), yc = yPrice(b.close);
      const top = Math.min(yo, yc);
      const height = Math.max(1, Math.abs(yc - yo));
      ctx.fillRect(x - candleW / 2, top, candleW, height);
    });

    // 均线
    const drawMa = (series, color) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      let started = false;
      series.forEach((v, i) => {
        if (v == null) return;
        const x = xAt(i), y = yPrice(v);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      });
      ctx.stroke();
    };
    drawMa(ma5, '#e8b339');
    drawMa(ma10, '#4d9de0');
    drawMa(ma20, '#c678dd');

    // 成交量
    const volTop = padTop + mainH + gap;
    ctx.strokeStyle = '#171e27';
    ctx.beginPath();
    ctx.moveTo(padL, volTop + volH);
    ctx.lineTo(padL + chartW, volTop + volH);
    ctx.stroke();
    bars.forEach((b, i) => {
      const x = xAt(i);
      const h = maxVol ? (b.volume / maxVol) * volH : 0;
      ctx.fillStyle = b.close >= b.open ? 'rgba(246,70,93,.55)' : 'rgba(46,189,133,.55)';
      ctx.fillRect(x - candleW / 2, volTop + volH - h, candleW, h);
    });
    ctx.fillStyle = '#5c6874';
    ctx.font = '10px monospace';
    ctx.fillText('成交量', padL + 2, volTop + 10);

    // 日期轴
    ctx.fillStyle = '#5c6874';
    ctx.font = '10.5px monospace';
    const labelCount = Math.min(6, bars.length);
    for (let k = 0; k < labelCount; k++) {
      const index = Math.floor((bars.length - 1) * (k / Math.max(1, labelCount - 1)));
      const d = String(bars[index].date);
      const label = d.slice(4, 6) + '/' + d.slice(6, 8);
      const x = xAt(index);
      ctx.fillText(label, Math.min(x - 14, cssWidth - 34), volTop + volH + 15);
    }
  }

  /* ------------------------------------------------------------ 事件绑定 */

  function bindEvents() {
    $('#btn-new-session').addEventListener('click', () => createSession());
    $('#btn-settings').addEventListener('click', () => { loadConfig(); $('#settings-modal').hidden = false; });
    $('#btn-memories').addEventListener('click', async () => {
      await loadMemories();
      $('#memories-modal').hidden = false;
    });

    // 回测
    $('#btn-bt-run').addEventListener('click', () => startBacktest('single'));
    $('#btn-bt-sweep').addEventListener('click', () => openGrid('sweep'));
    $('#btn-bt-wf').addEventListener('click', () => openGrid('walkforward'));
    $('#btn-bt-sweep-run').addEventListener('click', () => startBacktest(btState.mode || 'sweep'));
    $('#btn-bt-sweep-cancel').addEventListener('click', () => showBtPanels('config'));
    $('#btn-bt-add-exit').addEventListener('click', addExitCondition);
    $('#bt-exit-new').addEventListener('change', renderExitParams);
    // 结果页里的返回/重跑按钮（工具栏是动态渲染的，所以用事件代理）
    $('#backtest-modal').addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-bt-back]');
      if (!btn) return;
      const target = btn.dataset.btBack;
      if (target === 'config') {
        // 记下来：重开弹窗时不要再自动弹回结果页
        btState.dismissed = true;
        showBtPanels('config');
      } else if (target === 'grid') {
        openGrid(btn.dataset.mode || btState.mode || 'sweep');
      } else if (target === 'rerun') {
        startBacktest(btn.dataset.mode || 'single');
      } else if (target === 'list') {
        // 回到策略列表（打开回测时它被藏起来了）
        $('#backtest-modal').hidden = true;
        $('#strategy-modal').hidden = false;
      }
    });
    $('#bt-exits').addEventListener('click', (ev) => {
      const btn = ev.target.closest('button[data-idx]');
      if (!btn) return;
      btState.exits.splice(parseInt(btn.dataset.idx, 10), 1);
      renderExits();
    });

    // 自选股
    $('#btn-watchlist').addEventListener('click', async () => {
      $('#watchlist-modal').hidden = false;
      await loadWatchlist();
    });
    // 在通达信里加了自选股之后点它重新读取（服务端每次都直接重读 .blk 文件，没有缓存）
    $('#btn-watchlist-refresh').addEventListener('click', () => loadWatchlist());

    // 自选策略
    $('#btn-strategies').addEventListener('click', async () => {
      $('#strategy-run-result').innerHTML = '';
      closeStrategyForm();
      await loadStrategies();
      $('#strategy-modal').hidden = false;
    });
    $('#btn-strategy-new').addEventListener('click', () => openStrategyForm(null));
    $('#btn-strategy-save').addEventListener('click', saveStrategyForm);
    $('#btn-strategy-cancel').addEventListener('click', closeStrategyForm);
    $('#btn-strategy-search').addEventListener('click', () => {
      $('#strategy-run-result').innerHTML = '';
      loadStrategies($('#strategy-search').value.trim());
    });
    $('#strategy-search').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); $('#btn-strategy-search').click(); }
    });
    $('#strategy-list').addEventListener('click', (ev) => {
      const btn = ev.target.closest('button[data-act]');
      if (!btn) return;
      const item = btn.closest('.strategy-item');
      if (!item) return;
      const id = item.dataset.id;
      const nameEl = item.querySelector('.s-name');
      const name = nameEl ? nameEl.textContent : '';
      const action = btn.dataset.act;
      if (action === 'edit') {
        const found = state.strategies.find((s) => String(s.id) === String(id));
        if (found) openStrategyForm(found);
      } else if (action === 'del') {
        deleteStrategy(id, name);
      } else if (action === 'run') {
        runStrategy(id, name);
      } else if (action === 'backtest') {
        const found = state.strategies.find((s) => String(s.id) === String(id));
        if (found) openBacktest(found);
      }
    });

    // 知识库
    $('#btn-kb').addEventListener('click', async () => {
      await loadKb();
      $('#kb-modal').hidden = false;
    });
    $('#btn-kb-import').addEventListener('click', () => $('#kb-file').click());
    $('#kb-file').addEventListener('change', async (ev) => {
      await importKbFiles(ev.target.files);
      ev.target.value = '';
    });
    $('#btn-kb-search').addEventListener('click', kbSearch);
    $('#kb-query').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); kbSearch(); }
    });
    $('#btn-kb-clear').addEventListener('click', async () => {
      if (!confirm('确定清空知识库里的全部文档吗？此操作不可恢复。')) return;
      await api('/api/kb/clear', { method: 'POST' });
      await loadKb();
      $('#kb-search-result').innerHTML = '';
      toast('知识库已清空');
    });
    $('#kb-list').addEventListener('click', async (ev) => {
      const btn = ev.target.closest('.del');
      if (!btn) return;
      await api('/api/kb/' + btn.dataset.id, { method: 'DELETE' });
      await loadKb();
    });
    $('#btn-refresh').addEventListener('click', refreshData);
    $('#btn-save-config').addEventListener('click', saveConfig);
    $('#btn-verify').addEventListener('click', verifyKey);

    $('#usage-card').addEventListener('click', () => {
      $('#usage-modal').hidden = false;
      loadUsage(true);
    });
    $('#btn-refresh-usage').addEventListener('click', () => loadUsage(true));

    $('#btn-add-memory').addEventListener('click', async () => {
      const input = $('#new-memory');
      const content = input.value.trim();
      if (!content) return;
      await api('/api/memories', { method: 'POST', body: JSON.stringify({ content, kind: 'note' }) });
      input.value = '';
      loadMemories();
      toast('已加入长期记忆');
    });

    $('#memory-list').addEventListener('click', async (ev) => {
      const btn = ev.target.closest('.del');
      if (!btn) return;
      await api('/api/memories/' + btn.dataset.id, { method: 'DELETE' });
      loadMemories();
    });

    $('#btn-quit').addEventListener('click', async () => {
      if (!confirm('确定退出程序吗？退出后网页将无法使用。')) return;
      try { await api('/api/shutdown', { method: 'POST' }); } catch (e) { /* 程序退出会断开连接 */ }
      document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;' +
        'font-family:sans-serif;color:#9aa7b4;flex-direction:column;gap:10px">' +
        '<div style="font-size:16px;color:#e6edf3">程序已退出</div>' +
        '<div style="font-size:13px">可以关闭这个页面了</div></div>';
    });

    $('#session-list').addEventListener('click', (ev) => {
      const del = ev.target.closest('.del');
      const item = ev.target.closest('.session-item');
      if (!item) return;
      if (del) { ev.stopPropagation(); deleteSession(item.dataset.id); return; }
      if (item.dataset.id !== state.sessionId) switchSession(item.dataset.id);
    });

    // 表格行点击看 K 线：绑在 document 上，这样聊天消息区、自选股面板、
    // 策略试跑结果里的表格都能点。
    // （原来只绑在消息区，导致在面板里点股票行没反应）
    document.addEventListener('click', (ev) => {
      const row = ev.target.closest('tr[data-code]');
      if (row) showKline(row.dataset.code);
    });

    document.querySelectorAll('.suggestion').forEach((btn) => {
      btn.addEventListener('click', () => {
        $('#input').value = btn.dataset.q;
        updateSendButton();
        sendMessage(btn.dataset.q);
        $('#input').value = '';
        updateSendButton();
      });
    });

    document.querySelectorAll('[data-close]').forEach((btn) => {
      btn.addEventListener('click', () => { $('#' + btn.dataset.close).hidden = true; });
    });

    document.querySelectorAll('.modal').forEach((modal) => {
      modal.addEventListener('click', (ev) => { if (ev.target === modal) modal.hidden = true; });
    });

    const input = $('#input');
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(160, input.scrollHeight) + 'px';
      updateSendButton();
    });
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        const text = input.value;
        input.value = '';
        input.style.height = 'auto';
        updateSendButton();
        sendMessage(text);
      }
    });
    $('#btn-send').addEventListener('click', () => {
      const text = input.value;
      input.value = '';
      input.style.height = 'auto';
      updateSendButton();
      sendMessage(text);
    });

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') {
        document.querySelectorAll('.modal').forEach((m) => { m.hidden = true; });
      }
    });
  }

  /* ------------------------------------------------------------ 启动 */

  async function init() {
    bindEvents();
    await loadConfig();
    await loadSessions();
    if (!state.sessions.length) {
      await createSession();
    } else {
      await switchSession(state.sessions[0].id);
    }
    loadStatus();
    loadMemories();
    loadUsage();
    loadKb();
    loadStrategies();
    // 自选股只取数量做角标，不拉全量指标（省事）
    api('/api/watchlist?metrics=0').then((data) => {
      $('#watchlist-count').textContent = data.available ? (data.count || 0) : 0;
    }).catch(() => {});
    setInterval(loadStatus, 30000);
    setInterval(loadMemories, 60000);
    setInterval(loadUsage, 60000);
    updateSendButton();
  }

  init();
})();
