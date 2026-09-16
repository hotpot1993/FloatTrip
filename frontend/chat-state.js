// chat-state.js — Chat 页面可测试的实体化状态 reducer

(function (global) {
  const initialState = () => ({
    messages: {},
    messageOrder: [],
    briefs: {},
    runs: {},
    cursors: {},
  });

  const PRODUCT_STAGES = [
    { key: "understand", label: "理解旅行需求" },
    { key: "discover", label: "搜集目的地信息" },
    { key: "compose", label: "编排行程与优化路线" },
    { key: "polish", label: "完善旅行细节" },
  ];

  const INTERNAL_STAGE_MAP = {
    intent: "understand",
    query_rewrite: "understand",
    attraction_search: "discover",
    planner: "compose",
    reviewer: "compose",
    time_check: "compose",
    meal_search: "polish",
    meal_recommend: "polish",
    spot_tips: "polish",
    finalize: "polish",
  };
  const JOURNEY_STAGE_INDEX = {
    intent: 0,
    query_rewrite: 1,
    attraction_search: 2,
    planner: 3,
    reviewer: 3,
    time_check: 4,
    meal_search: 5,
    meal_recommend: 5,
    spot_tips: 6,
    finalize: 6,
  };

  const RUN_PRESENTATIONS = {
    queued: {
      label: "等待开始",
      copy: "任务已经保存，会在资源可用时自动开始。",
      primaryAction: "cancel",
    },
    running: {
      label: "正在规划",
      copy: "你可以继续聊天或离开页面，规划会在后台继续。",
      primaryAction: "cancel",
    },
    waiting_user: {
      label: "需要你的回复",
      copy: "规划暂时停在这里，收到你的回答后会从原处继续。",
      primaryAction: "resume",
    },
    succeeded: {
      label: "行程已经准备好",
      copy: "我已经把路线、时间和旅行细节整理成一份完整行程。",
      primaryAction: "open",
    },
    failed: {
      label: "这次没有完成",
      copy: "原始需求仍然保留，可以直接再试一次。",
      primaryAction: "retry",
    },
    cancelled: {
      label: "规划已停止",
      copy: "任务已经停止，原始需求仍然保留。",
      primaryAction: "retry",
    },
  };

  function planningBriefStatusLabel(status) {
    return {
      collecting: "信息收集中",
      ready: "等待确认",
      submitted: "已提交",
      discarded: "已放弃",
    }[status] || status;
  }

  function conversationAttention(conversation) {
    if (!conversation) return null;
    if (conversation.status === "archived") {
      return { kind: "archived", label: "已归档", ariaLabel: "对话已归档" };
    }
    if (conversation.has_waiting_user) {
      return { kind: "waiting-user", label: "待你回复", ariaLabel: "规划正在等待你的回复" };
    }
    if (conversation.has_ready_brief) {
      return { kind: "ready-brief", label: "待确认", ariaLabel: "旅行方案等待你确认" };
    }
    if (conversation.has_active_planning) {
      return { kind: "planning", label: "规划中", ariaLabel: "旅行行程正在规划" };
    }
    if (conversation.has_unread_completed) {
      return { kind: "unread", label: "新行程", ariaLabel: "有尚未查看的新行程" };
    }
    return null;
  }

  function shouldMarkConversationViewed(visibilityState, activeConversationId, conversationId) {
    return visibilityState === "visible"
      && !!activeConversationId
      && activeConversationId === conversationId;
  }

  function shouldPollConversations(visibilityState) {
    return visibilityState === "visible";
  }

  function upsertMessage(state, message) {
    const exists = !!state.messages[message.id];
    return {
      ...state,
      messages: { ...state.messages, [message.id]: { ...state.messages[message.id], ...message } },
      messageOrder: exists
        ? state.messageOrder
        : [...state.messageOrder, message.id].sort((a, b) => {
            const left = state.messages[a]?.sequence ?? message.sequence ?? Number.MAX_SAFE_INTEGER;
            const right = state.messages[b]?.sequence ?? message.sequence ?? Number.MAX_SAFE_INTEGER;
            return left - right;
          }),
    };
  }

  function productStageFor(stage) {
    return INTERNAL_STAGE_MAP[stage] || null;
  }

  function interactionInputKind(interaction) {
    const schema = interaction?.input_schema || {};
    const question = String(interaction?.question || "");
    if (schema.format === "date-range" || /日期|开始.*结束|start_date|end_date/.test(question)) {
      return "date-range";
    }
    if (Array.isArray(schema.enum)) return "single-choice";
    if (schema.type === "array" && Array.isArray(schema.items?.enum)) return "multi-choice";
    return "text";
  }

  function memoryFactPresentation(fact) {
    const contextOnly = fact?.application_level === "context_only";
    const polarity = contextOnly ? "fact" : (fact?.polarity || "fact");
    return {
      prefer: {
        tone: "prefer", badge: "优先考虑", summaryLabel: "偏好",
        effect: "规划时会优先考虑", excludeAction: "本次不优先", restoreAction: "恢复优先",
      },
      avoid: {
        tone: "avoid", badge: "本次避开", summaryLabel: "避开",
        effect: "规划时将排除，不纳入候选行程", excludeAction: "本次允许安排", restoreAction: "恢复避开",
      },
      require: {
        tone: "require", badge: "必须满足", summaryLabel: "必须",
        effect: "将作为本次行程的硬性要求", excludeAction: "本次取消要求", restoreAction: "恢复要求",
      },
      fact: {
        tone: "context", badge: "仅作背景", summaryLabel: "背景",
        effect: "只用于理解行程，不代表要安排", excludeAction: "本次不参考", restoreAction: "恢复参考",
      },
    }[polarity] || {
      tone: "context", badge: "仅作背景", summaryLabel: "背景",
      effect: "只用于理解行程，不代表要安排", excludeAction: "本次不参考", restoreAction: "恢复参考",
    };
  }

  // 抵达与返程时刻可能是模糊表述（「傍晚」）。摘要必须原样展示用户说的话，
  // 同时标明它不是具体钟点——后端据此采用保守下界，用户看到的是同一个事实。
  // 匹配范围必须与后端 helpers._parse_clock 一致：它用的是 search，
  // 所以「下午3点」在两端都算具体时刻，不能只认以数字开头的写法。
  function isConcreteTime(value) {
    const text = (value || "").trim();
    return /\d{1,2}[:：]\d{2}/.test(text) || /\d{1,2}\s*[点时]/.test(text);
  }

  function timeWindowPart(value) {
    const text = (value || "").trim();
    if (!text) return "未提供";
    return isConcreteTime(text) ? text : `${text}（未提供具体时刻）`;
  }

  // 提问清单的「哪些字段、什么顺序」由服务端说了算（brief.answered_fields /
  // brief.question），前端只负责把它们说成人话。服务端将来加了字段而这里还
  // 没跟上时，标签会退化成字段名而不是报错。
  const BRIEF_FIELD_LABELS = {
    destination: "目的地",
    date_range: "出行日期",
    arrival_time: "抵达时刻",
    departure_time: "返程时刻",
    trip_budget: "本次预算",
    preferences: "旅行偏好",
  };

  function briefFieldValue(field, data) {
    const payload = data || {};
    if (field === "destination") return String(payload.destination || "").trim();
    if (field === "date_range") {
      if (payload.start_date && payload.end_date) {
        return `${payload.start_date} — ${payload.end_date}`;
      }
      return payload.days ? `${payload.days} 天` : "";
    }
    if (field === "arrival_time" || field === "departure_time") {
      return String(payload[field] || "").trim();
    }
    if (field === "trip_budget") {
      return String(payload.trip_budget || payload.budget || "").trim();
    }
    if (field === "preferences") {
      return (payload.trip_constraints || [])
        .map(item => String(item?.value_text || "").trim())
        .filter(Boolean)
        .join("、");
    }
    return "";
  }

  // brief.answered_fields 已经按清单顺序列出「已答或已跳过」的字段，
  // 因此这里不再自己排序，也不会漏掉任何一项。
  function briefFieldRecords(brief) {
    const data = brief?.data || {};
    const declined = new Set(brief?.declined_fields || []);
    return (brief?.answered_fields || []).map(field => {
      const skipped = declined.has(field);
      return {
        field,
        label: BRIEF_FIELD_LABELS[field] || field,
        skipped,
        value: skipped ? "" : briefFieldValue(field, data),
      };
    });
  }

  function briefProgressLabel(brief) {
    const question = brief?.question;
    if (!question || brief?.collected) return "";
    const remaining = Number(question.remaining || 0);
    return remaining > 0 ? `还差 ${remaining} 项` : "";
  }

  // 一条 brief 派生出多个时间线条目：每个已处理字段一行「已记录」、当前提问
  // 一张卡片、收集完成后一张需求摘要。key 全部由 brief id 与字段名拼成，
  // 因此事件重放或刷新重建都不会产生第二份。
  function briefDerivedItems(entity) {
    const anchor = entity.created_at || entity.updated_at || "";
    const items = [];
    briefFieldRecords(entity).forEach((record, index) => {
      items.push({
        key: `brief-record:${entity.id}:${record.field}`,
        type: "brief_record",
        entityId: entity.id,
        entity: { ...record, briefId: entity.id },
        createdAt: anchor,
        sequence: Number.MAX_SAFE_INTEGER,
        derivedOrder: index,
      });
    });
    if (entity.question && !entity.collected) {
      items.push({
        key: `brief-question:${entity.id}:${entity.question.field}`,
        type: "brief_question",
        entityId: entity.id,
        entity: {
          ...entity.question,
          briefId: entity.id,
          status: entity.status,
          progress: briefProgressLabel(entity),
          options: entity.question.options || [],
        },
        createdAt: anchor,
        sequence: Number.MAX_SAFE_INTEGER,
        derivedOrder: items.length,
      });
    }
    if (entity.collected) {
      items.push({
        key: `brief:${entity.id}`,
        type: "brief",
        entityId: entity.id,
        entity,
        createdAt: anchor,
        sequence: Number.MAX_SAFE_INTEGER,
        derivedOrder: items.length,
      });
    }
    return items;
  }

  function briefViewModel(brief) {
    const data = brief?.data || {};
    const missingLabels = {
      destination: "目的地",
      start_date: "开始日期",
      end_date: "结束日期",
      date_range: "有效的日期范围",
      dates_or_days: "具体出行日期",
    };
    const categoryLabels = {
      attraction_preference: "景点", food_preference: "餐饮", dietary_requirement: "饮食要求",
      travel_pace: "旅行节奏", budget_style: "预算习惯", transport_preference: "交通",
      accommodation_preference: "住宿", schedule_preference: "作息", companion_context: "同行",
      accessibility_need: "无障碍", other_travel_preference: "其他",
    };
    const preferences = (data.trip_constraints || []).map(item => {
      const presentation = memoryFactPresentation(item);
      return [
        `${categoryLabels[item.category] || "旅行要求"} · ${presentation.summaryLabel}`,
        item.value_text,
      ];
    });
    [["景点", data.attraction_preference], ["餐饮", data.food_preference], ["旅行节奏", data.habit_preference]]
      .forEach(([label, value]) => {
        if (value && !preferences.some(([, existing]) => existing === value)) preferences.push([label, value]);
      });
    if (data.trip_budget || data.budget) preferences.unshift(["本次预算", data.trip_budget || data.budget]);
    const arrivalLabel = timeWindowPart(data.arrival_time);
    const departureLabel = timeWindowPart(data.departure_time);
    return {
      destination: data.destination || "还没决定",
      dateLabel: data.start_date && data.end_date
        ? `${data.start_date} — ${data.end_date}`
        : data.days ? `${data.days} 天 · 日期待定` : "日期待补充",
      arrivalLabel,
      departureLabel,
      timeWindowLabel: arrivalLabel === "未提供" && departureLabel === "未提供"
        ? "未提供"
        : `抵达 ${arrivalLabel} · 返程 ${departureLabel}`,
      preferences,
      usesDefaults: brief?.status === "ready"
        && !(data.trip_constraints || []).length
        && !(brief?.memory_context?.applied_facts || []).length,
      missing: (brief?.missing_fields || []).map(field => missingLabels[field] || field),
    };
  }

  function advanceRunStage(run, internalStage, label) {
    const key = productStageFor(internalStage);
    if (!key) return { ...run, latest_progress_label: label || run.latest_progress_label };
    const incomingIndex = PRODUCT_STAGES.findIndex(stage => stage.key === key);
    const currentIndex = PRODUCT_STAGES.findIndex(stage => stage.key === run.product_stage);
    const nextIndex = Math.max(incomingIndex, currentIndex, 0);
    const incomingJourneyIndex = JOURNEY_STAGE_INDEX[internalStage];
    const currentJourneyIndex = Number.isInteger(run.journey_step_index)
      ? run.journey_step_index
      : -1;
    return {
      ...run,
      product_stage: PRODUCT_STAGES[nextIndex].key,
      completed_product_stages: PRODUCT_STAGES
        .slice(0, nextIndex)
        .map(stage => stage.key),
      journey_step_index: Number.isInteger(incomingJourneyIndex)
        ? Math.max(incomingJourneyIndex, currentJourneyIndex)
        : currentJourneyIndex,
      internal_stage: Number.isInteger(incomingJourneyIndex)
        && incomingJourneyIndex >= currentJourneyIndex
        ? internalStage
        : run.internal_stage,
      latest_progress_label: label || run.latest_progress_label,
    };
  }

  function activityItems(state) {
    const items = [];
    state.messageOrder.forEach((id) => {
      const entity = state.messages[id];
      if (entity) items.push({
        key: `message:${id}`,
        type: "message",
        entityId: id,
        entity,
        clarification: entity.role === "assistant"
          ? (state.runs[entity.related_run_id]?.clarification || null)
          : null,
        createdAt: entity.created_at || "",
        sequence: Number(entity.sequence || Number.MAX_SAFE_INTEGER),
      });
    });
    Object.values(state.briefs).forEach((entity) => {
      if (!entity || entity.status === "discarded") return;
      briefDerivedItems(entity).forEach(item => items.push(item));
    });
    Object.entries(state.runs).forEach(([runKey, stored]) => {
      if (!stored) return;
      // 事件可能先于 listRuns 到达，此时 runs[runId] 只有事件写入的字段、没有 id。
      // 用 map 的键兜底，否则派生的 key 会变成 "run:undefined"。
      const entity = stored.id ? stored : { ...stored, id: runKey };
      if (entity.kind === "chat") {
        if (["queued", "running"].includes(entity.status)) {
          items.push({
            key: `chat-thinking:${entity.id}`,
            type: "chat_thinking",
            entityId: entity.id,
            entity,
            createdAt: entity.created_at || entity.queued_at || entity.updated_at || "",
            sequence: Number.MAX_SAFE_INTEGER,
          });
          return;
        }
        if (entity.status !== "failed") return;
        items.push({
          key: `chat-failure:${entity.id}`,
          type: "chat_failure",
          entityId: entity.id,
          entity,
          createdAt: entity.created_at || entity.queued_at || entity.updated_at || "",
          sequence: Number.MAX_SAFE_INTEGER,
        });
        return;
      }
      items.push({
        key: `run:${entity.id}`,
        type: "run",
        entityId: entity.id,
        entity,
        createdAt: entity.created_at || entity.queued_at || entity.updated_at || "",
        sequence: Number.MAX_SAFE_INTEGER,
      });
      // 配额预警是这条 Run 的旁路信息，不是独立实体：挂在同一 entityId 上，
      // 靠 derivedOrder 保证它排在任务卡之前。没有预警时不产生任何条目。
      if (entity.quota_warning) {
        items.push({
          key: `quota-warning:${entity.id}`,
          type: "quota_warning",
          entityId: entity.id,
          entity,
          createdAt: entity.created_at || entity.queued_at || entity.updated_at || "",
          sequence: Number.MAX_SAFE_INTEGER,
          derivedOrder: -1,
        });
      }
    });
    const typePriority = {
      message: 0, chat_thinking: 1,
      brief_record: 2, brief_question: 3, brief: 4,
      quota_warning: 5, run: 6, chat_failure: 7,
    };
    return items.sort((left, right) => {
      if (left.type === "message" && right.type === "message" && left.sequence !== right.sequence) {
        return left.sequence - right.sequence;
      }
      if (left.createdAt && right.createdAt && left.createdAt !== right.createdAt) {
        return left.createdAt.localeCompare(right.createdAt);
      }
      if (left.createdAt !== right.createdAt) return left.createdAt ? -1 : 1;
      if (left.sequence !== right.sequence) return left.sequence - right.sequence;
      // 同一条 brief 派生出的条目共享锚点与 sequence，只能靠派生顺序区分：
      // 已记录在前、当前提问次之、需求摘要最后。
      if (
        Number.isInteger(left.derivedOrder)
        && Number.isInteger(right.derivedOrder)
        && left.entityId === right.entityId
      ) {
        return left.derivedOrder - right.derivedOrder;
      }
      if (typePriority[left.type] !== typePriority[right.type]) {
        return typePriority[left.type] - typePriority[right.type];
      }
      return left.key.localeCompare(right.key);
    });
  }

  function applyEvent(state, runId, event) {
    const seq = Number(event.sequence || 0);
    if (seq && seq <= Number(state.cursors[runId] || 0)) return state;
    let next = seq
      ? { ...state, cursors: { ...state.cursors, [runId]: seq } }
      : state;
    const payload = event.payload || {};
    if (event.kind === "messages") {
      const id = payload.message_id || `assistant:${runId}`;
      const current = next.messages[id] || {
        id, role: "assistant", content: "", streaming: true, related_run_id: runId,
      };
      next = upsertMessage(next, {
        ...current,
        content: current.content + (payload.delta || ""),
      });
    } else if (event.kind === "custom") {
      if (payload.kind === "chat.message.completed") {
        const tempId = `assistant:${runId}`;
        const withoutTemp = { ...next.messages };
        delete withoutTemp[tempId];
        next = {
          ...next,
          messages: withoutTemp,
          messageOrder: next.messageOrder.filter(id => id !== tempId),
        };
        next = upsertMessage(next, {
          id: payload.message_id,
          role: "assistant",
          content: payload.content,
          sequence: payload.sequence,
          created_at: payload.created_at,
          related_run_id: runId,
          streaming: false,
        });
      } else if (String(payload.kind || "").startsWith("planning_brief.")) {
        const previous = next.briefs[payload.brief_id] || {};
        const incoming = {
          ...previous,
          id: payload.brief_id,
          status: payload.status,
          data: payload.summary || {},
          missing_fields: payload.missing_fields || [],
          memory_context: payload.memory_context || previous.memory_context,
          effective_constraints: payload.effective_constraints || [],
          constraint_coverage: payload.constraint_coverage || [],
        };
        // 提问投影只在这条事件确实带了它的时候覆盖。终态事件（submitted /
        // discarded）不带提问，用空值覆盖会把已有的跳过记录抹掉，表现为
        // 用户重启对话后被重新问一遍。
        ["question", "collected", "declined_fields", "answered_fields"].forEach(key => {
          if (key in payload) incoming[key] = payload[key];
        });
        next = {
          ...next,
          briefs: { ...next.briefs, [payload.brief_id]: incoming },
        };
      } else if (payload.kind === "chat.clarification") {
        // 澄清的候选项是实时增强：它不落库，刷新后同一条助手消息退化成
        // 普通气泡，问题文本本身不会丢。
        next = {
          ...next,
          runs: {
            ...next.runs,
            [runId]: { ...(next.runs[runId] || {}), clarification: payload },
          },
        };
      } else if (payload.kind === "run.created" && payload.run?.id) {
        const created = payload.run;
        next = {
          ...next,
          runs: {
            ...next.runs,
            [created.id]: { ...next.runs[created.id], ...created },
          },
        };
      } else {
        const currentRun = next.runs[runId] || {};
        let pendingInteraction = currentRun.pending_interaction;
        if (payload.kind === "run.waiting_user") {
          pendingInteraction = payload;
        } else if (
          payload.kind === "run.status"
          && payload.status !== "waiting_user"
        ) {
          pendingInteraction = null;
        }
        const stagedRun = payload.kind === "planning_run.progress"
          ? advanceRunStage(currentRun, payload.stage, payload.label)
          : currentRun;
        // 高德配额预警：整轮规划只需要知道「当前有没有未处理的预警」，
        // 所以只留最后一条，不堆叠历史。
        const quotaWarning = payload.kind === "amap.quota_warning"
          ? {
              bucket: payload.bucket,
              used: payload.used,
              limit: payload.limit,
              remaining: payload.remaining,
              message: payload.message,
            }
          : currentRun.quota_warning;
        const itineraryResult = payload.kind === "planning.itinerary_created"
          ? {
              result_itinerary_id: payload.itinerary_id,
              request_snapshot: {
                ...(currentRun.request_snapshot || {}),
                ...(payload.destination && !currentRun.request_snapshot?.destination
                  ? { destination: payload.destination }
                  : {}),
              },
            }
          : {};
        next = {
          ...next,
          runs: {
            ...next.runs,
            [runId]: {
              ...stagedRun,
              ...itineraryResult,
              ...(payload.kind === "run.status" ? { status: payload.status } : {}),
              pending_interaction: pendingInteraction,
              quota_warning: quotaWarning,
              last_event: payload,
            },
          },
        };
      }
    } else if (event.kind === "end") {
      next = {
        ...next,
        runs: {
          ...next.runs,
          [runId]: { ...next.runs[runId], status: payload.status || next.runs[runId]?.status },
        },
      };
    }
    return next;
  }

  global.ChatState = {
    initialState,
    upsertMessage,
    applyEvent,
    activityItems,
    advanceRunStage,
    productStageFor,
    interactionInputKind,
    memoryFactPresentation,
    briefViewModel,
    briefFieldRecords,
    briefFieldValue,
    briefProgressLabel,
    briefDerivedItems,
    BRIEF_FIELD_LABELS,
    RUN_PRESENTATIONS,
    PRODUCT_STAGES,
    planningBriefStatusLabel,
    conversationAttention,
    shouldMarkConversationViewed,
    shouldPollConversations,
  };
})(typeof window === "undefined" ? globalThis : window);
