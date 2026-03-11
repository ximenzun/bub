import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

const BRIDGE_PROTOCOL_VERSION = "1";

function emit(frame) {
  process.stdout.write(`${JSON.stringify(frame)}\n`);
}

function buildReady(channel, extra = {}) {
  return { type: "ready", version: BRIDGE_PROTOCOL_VERSION, channel, ...extra };
}

function buildState(state, extra = {}) {
  return { type: "state", version: BRIDGE_PROTOCOL_VERSION, state, ...extra };
}

function buildLog(message, level = "info", extra = {}) {
  return { type: "log", version: BRIDGE_PROTOCOL_VERSION, level, message, ...extra };
}

function buildProvisioning(info) {
  return { type: "provisioning", version: BRIDGE_PROTOCOL_VERSION, provisioning: info };
}

function tempFileName(prefix, fallbackExt = "bin") {
  const stamp = Date.now();
  return path.join(os.tmpdir(), `${prefix}-${stamp}.${fallbackExt}`);
}

async function writeTempFile(buffer, suggestedName, fallbackExt = "bin") {
  const name = suggestedName || tempFileName("bub-wecom", fallbackExt);
  const target = path.isAbsolute(name) ? name : tempFileName(name.replace(/[^\w.-]/g, "_"), fallbackExt);
  await fs.writeFile(target, buffer);
  return target;
}

function parseArgs(argv) {
  const args = {
    channel: "wecom_longconn_bot",
    chatId: "wecom-dev-chat",
    bootMessage: "",
    mock: false,
    echoActions: false,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === "--channel") args.channel = argv[++i];
    else if (token === "--chat-id") args.chatId = argv[++i];
    else if (token === "--boot-message") args.bootMessage = argv[++i];
    else if (token === "--mock") args.mock = true;
    else if (token === "--echo-actions") args.echoActions = true;
  }
  return args;
}

function inferWeComMsgtype(action) {
  if (action.card) return "template_card";
  if (action.content_type === "card") return "template_card";
  if (action.content_type === "image") return "image";
  if (action.content_type === "audio") return "voice";
  if (action.content_type === "file") return "file";
  if (action.content_type === "rich_text") return "markdown";
  return "text";
}

function buildMentionLists(mentions = []) {
  const mentioned_list = [];
  const mentioned_mobile_list = [];
  for (const mention of mentions) {
    if (mention.kind === "user_id") mentioned_list.push(mention.value);
    else if (mention.kind === "mobile") mentioned_mobile_list.push(mention.value);
    else if (mention.kind === "all") {
      mentioned_list.push("@all");
      mentioned_mobile_list.push("@all");
    }
  }
  return { mentioned_list, mentioned_mobile_list };
}

function buildTextPayload(action) {
  const { mentioned_list, mentioned_mobile_list } = buildMentionLists(action.mentions);
  const payload = { content: action.text || "" };
  if (mentioned_list.length > 0) payload.mentioned_list = mentioned_list;
  if (mentioned_mobile_list.length > 0) payload.mentioned_mobile_list = mentioned_mobile_list;
  return payload;
}

function buildMarkdownContent(action) {
  const { mentioned_list, mentioned_mobile_list } = buildMentionLists(action.mentions);
  const markdownContent = [action.text || ""];
  if (mentioned_list.length > 0) markdownContent.push(`\n${mentioned_list.map((item) => `<@${item}>`).join(" ")}`);
  if (mentioned_mobile_list.length > 0) markdownContent.push(`\n${mentioned_mobile_list.join(" ")}`);
  return markdownContent.join("");
}

function templateCardOf(action) {
  if (action.card && typeof action.card === "object" && !Array.isArray(action.card)) {
    return action.card;
  }
  throw new Error("WeCom template cards require action.card.");
}

function replyContextOf(action) {
  const grant = action.reply_grant && typeof action.reply_grant === "object" ? action.reply_grant : {};
  const metadata = grant.metadata && typeof grant.metadata === "object" ? grant.metadata : {};
  return {
    reqId: grant.token || null,
    eventType: metadata.event_type || null,
    responseUrl: metadata.response_url || null,
  };
}

function firstNonEmptyString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return null;
}

function resolveChatTarget(body, state) {
  const senderUserId = firstNonEmptyString(
    body.from?.userid,
    body.userid,
    body.from_userid,
    body.external_userid,
    body.externalUserid,
    body.sender_userid,
  );
  const candidates = [
    ["chatid", body.chatid],
    ["from.userid", body.from?.userid],
    ["userid", body.userid],
    ["from_userid", body.from_userid],
    ["external_userid", body.external_userid],
    ["externalUserid", body.externalUserid],
    ["sender_userid", body.sender_userid],
  ];
  for (const [source, value] of candidates) {
    const text = firstNonEmptyString(value);
    if (text) {
      return { chatId: text, source, senderUserId };
    }
  }
  return { chatId: state.chatId, source: "state.chatId", senderUserId };
}

function streamIdOf(action) {
  return action.live_surface?.surface_id || `stream_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeTargetIds(action) {
  return Array.isArray(action.target_ids) && action.target_ids.length > 0 ? action.target_ids.map((item) => String(item)) : undefined;
}

async function readBinarySource(source, suggestedName = null) {
  if (!source) throw new Error("missing outbound binary source");
  if (source.startsWith("data:")) {
    const [header, encoded = ""] = source.split(",", 2);
    const raw = header.includes(";base64") ? Buffer.from(encoded, "base64") : Buffer.from(decodeURIComponent(encoded), "utf8");
    return { buffer: raw, filename: suggestedName };
  }
  if (source.startsWith("http://") || source.startsWith("https://")) {
    const response = await fetch(source);
    if (!response.ok) {
      throw new Error(`failed to fetch outbound attachment: ${response.status} ${response.statusText}`);
    }
    const url = new URL(source);
    return {
      buffer: Buffer.from(await response.arrayBuffer()),
      filename: suggestedName || path.basename(url.pathname) || null,
    };
  }
  const localPath = source.startsWith("file://") ? fileURLToPath(source) : source;
  return { buffer: await fs.readFile(localPath), filename: suggestedName || path.basename(localPath) || null };
}

async function readActionBinarySource(action) {
  const attachment = Array.isArray(action.attachments) && action.attachments.length > 0 ? action.attachments[0] : null;
  const attachmentPath = attachment?.metadata?.path ? String(attachment.metadata.path) : null;
  const source = attachment?.url || attachmentPath || action.text || null;
  if (!source) {
    throw new Error("WeCom outbound media actions require an attachment or a text source path/URL.");
  }
  return readBinarySource(String(source), attachment?.name || null);
}

async function buildImageReplyItem(action) {
  const { buffer } = await readActionBinarySource(action);
  return {
    msgtype: "image",
    image: {
      base64: buffer.toString("base64"),
      md5: createHash("md5").update(buffer).digest("hex"),
    },
  };
}

function buildWelcomeBody(action) {
  if (action.card) {
    return { msgtype: "template_card", template_card: templateCardOf(action) };
  }
  return { msgtype: "text", text: buildTextPayload(action) };
}

async function buildPassiveReplyRequest(action, frameHeaders, msgtype) {
  if (msgtype === "template_card") {
    const templateCard = templateCardOf(action);
    if ((action.text || "").trim()) {
      return {
        mode: "passive_reply",
        op: "replyStreamWithCard",
        args: [frameHeaders, streamIdOf(action), action.text || "", true, { templateCard }],
      };
    }
    return { mode: "passive_reply", op: "replyTemplateCard", args: [frameHeaders, templateCard] };
  }
  if (msgtype === "text") {
    return {
      mode: "passive_reply",
      op: "replyStream",
      args: [frameHeaders, streamIdOf(action), action.text || "", true],
    };
  }
  if (msgtype === "markdown") {
    return {
      mode: "passive_reply",
      op: "replyStream",
      args: [frameHeaders, streamIdOf(action), buildMarkdownContent(action), true],
    };
  }
  if (msgtype === "image") {
    return {
      mode: "passive_reply",
      op: "replyStream",
      args: [frameHeaders, streamIdOf(action), action.text || "", true, [await buildImageReplyItem(action)]],
    };
  }
  throw new Error(`WeCom long-connection does not support passive outbound msgtype '${msgtype}'.`);
}

function buildProactiveRequest(action, msgtype) {
  if (msgtype === "template_card") {
    return {
      mode: "proactive_reply",
      op: "sendMessage",
      args: [action.conversation?.chat_id, { msgtype: "template_card", template_card: templateCardOf(action) }],
    };
  }
  if (msgtype === "text" || msgtype === "markdown") {
    return {
      mode: "proactive_reply",
      op: "sendMessage",
      args: [action.conversation?.chat_id, { msgtype: "markdown", markdown: { content: buildMarkdownContent(action) } }],
    };
  }
  throw new Error(`WeCom long-connection does not support proactive outbound msgtype '${msgtype}'.`);
}

function buildFallbackRequest(action, msgtype) {
  if (msgtype === "text" || msgtype === "markdown" || msgtype === "template_card") {
    return buildProactiveRequest(action, msgtype);
  }
  return null;
}

async function actionToRequest(action) {
  const msgtype = inferWeComMsgtype(action);
  const { reqId, eventType } = replyContextOf(action);
  const frameHeaders = reqId ? { headers: { req_id: reqId } } : null;

  if (action.kind === "edit_message") {
    throw new Error("WeCom long-connection does not support edit_message. Use update_card.");
  }

  if (action.kind === "update_card") {
    if (!frameHeaders) {
      throw new Error("WeCom update_card requires a reply_grant token.");
    }
    return {
      mode: "event_update",
      op: "updateTemplateCard",
      args: [frameHeaders, templateCardOf(action), normalizeTargetIds(action)],
    };
  }

  if (eventType === "enter_chat" && frameHeaders) {
    if (msgtype !== "text" && msgtype !== "markdown" && msgtype !== "template_card") {
      throw new Error(`WeCom welcome replies do not support msgtype '${msgtype}'.`);
    }
    return { mode: "welcome", op: "replyWelcome", args: [frameHeaders, buildWelcomeBody(action)] };
  }

  if (frameHeaders) {
    const request = await buildPassiveReplyRequest(action, frameHeaders, msgtype);
    const fallback = buildFallbackRequest(action, msgtype);
    if (fallback) {
      request.fallback = fallback;
    }
    return request;
  }

  return buildProactiveRequest(action, msgtype);
}

function isReplyAckError(error, errcode) {
  return Boolean(error && typeof error === "object" && error.errcode === errcode);
}

function renderError(error) {
  if (error instanceof Error) {
    return error.message;
  }
  if (error && typeof error === "object") {
    return JSON.stringify(error);
  }
  return String(error);
}

async function parseIncomingFrame(frame, wsClient, state) {
  const body = frame.body || {};
  const headers = frame.headers || {};
  const reqId = headers.req_id;
  const target = resolveChatTarget(body, state);

  emit(buildLog("incoming frame target resolved", "info", {
    chatId: target.chatId,
    source: target.source,
    senderUserId: target.senderUserId,
    chattype: body.chattype || null,
    msgtype: body.msgtype || frame.cmd || null,
    msgid: body.msgid || null,
    bodyKeys: Object.keys(body),
  }));

  if (target.source === "state.chatId") {
    emit(buildLog("incoming frame missing chat target, using state default", "warning", {
      msgtype: body.msgtype || frame.cmd || "unknown",
      bodyKeys: Object.keys(body),
      senderUserId: target.senderUserId,
      fallbackChatId: state.chatId,
    }));
  }

  if (frame.cmd === "aibot_event_callback" || body.msgtype === "event") {
    const eventType = body.event?.eventtype || "event";
    if (eventType === "disconnected_event") {
      emit(buildLog("ignored disconnected_event callback", "warning", {
        msgid: body.msgid || null,
        bodyKeys: Object.keys(body),
      }));
      return;
    }
    const chatId = target.chatId;
    const sessionId = `${state.channel}:${chatId}`;
    emit({
      type: "message",
      version: BRIDGE_PROTOCOL_VERSION,
      message: {
        session_id: sessionId,
        channel: state.channel,
        chat_id: chatId,
        content: `[WeCom event:${eventType}]`,
        is_active: true,
        message_id: body.msgid,
        conversation: {
          platform: "wecom",
          route_channel: state.channel,
          adapter_mode: "bridge",
          transport: "long_connection",
          chat_id: chatId,
          surface: body.chattype === "group" ? "group" : "direct",
          metadata: {
            wecom_chat_id_source: target.source,
            wecom_sender_userid: target.senderUserId,
          },
        },
        sender: {
          id: target.senderUserId || "unknown",
          id_kind: "wecom_userid",
        },
        reply_grant: {
          mode: "token",
          token: reqId,
          reply_to_message_id: body.msgid,
          metadata: {
            event_type: eventType,
          },
        },
        metadata: {
          wecom_raw_msgtype: "event",
          wecom_chat_id_source: target.source,
          wecom_sender_userid: target.senderUserId,
        },
      },
    });
    return;
  }

  const chatId = target.chatId;
  const sessionId = `${state.channel}:${chatId}`;
  const textParts = [];
  const attachments = [];

  if (body.text?.content) textParts.push(body.text.content);
  if (body.voice?.content) textParts.push(body.voice.content);
  if (body.mixed?.msg_item) {
    for (const item of body.mixed.msg_item) {
      if (item.msgtype === "text" && item.text?.content) textParts.push(item.text.content);
      if (item.msgtype === "image" && item.image?.url) {
        const localPath = await maybeDownloadAttachment(wsClient, item.image.url, item.image.aeskey, "image");
        attachments.push({
          content_type: "image/*",
          url: localPath || item.image.url,
          metadata: { remote_url: item.image.url, aeskey: item.image.aeskey || null },
        });
      }
    }
  }
  if (body.image?.url) {
    const localPath = await maybeDownloadAttachment(wsClient, body.image.url, body.image.aeskey, "image");
    attachments.push({
      content_type: "image/*",
      url: localPath || body.image.url,
      metadata: { remote_url: body.image.url, aeskey: body.image.aeskey || null },
    });
  }
  if (body.file?.url) {
    const localPath = await maybeDownloadAttachment(wsClient, body.file.url, body.file.aeskey, "file");
    attachments.push({
      content_type: "application/octet-stream",
      url: localPath || body.file.url,
      metadata: { remote_url: body.file.url, aeskey: body.file.aeskey || null },
    });
  }

  emit({
    type: "message",
    version: BRIDGE_PROTOCOL_VERSION,
    message: {
      session_id: sessionId,
      channel: state.channel,
      chat_id: chatId,
      content: textParts.join("\n") || `[WeCom ${body.msgtype || "message"}]`,
      is_active: true,
      message_id: body.msgid,
      conversation: {
        platform: "wecom",
        route_channel: state.channel,
        adapter_mode: "bridge",
        transport: "long_connection",
        chat_id: chatId,
        surface: body.chattype === "group" ? "group" : "direct",
        metadata: {
          wecom_chat_id_source: target.source,
          wecom_sender_userid: target.senderUserId,
        },
      },
      sender: {
        id: target.senderUserId || "unknown",
        id_kind: "wecom_userid",
      },
      reply_grant: {
        mode: "token",
        token: reqId,
        reply_to_message_id: body.msgid,
        metadata: {
          response_url: body.response_url || null,
          raw_msgtype: body.msgtype || null,
        },
      },
      attachments,
      metadata: {
        wecom_raw_msgtype: body.msgtype || null,
        wecom_chat_id_source: target.source,
        wecom_sender_userid: target.senderUserId,
      },
    },
  });
}

async function maybeDownloadAttachment(wsClient, url, aeskey, kind) {
  try {
    const { buffer, filename } = await wsClient.downloadFile(url, aeskey);
    const ext = filename?.split(".").pop() || (kind === "image" ? "img" : "bin");
    return await writeTempFile(buffer, filename, ext);
  } catch (error) {
    emit(buildLog("attachment download failed", "warning", { url, error: String(error) }));
    return null;
  }
}

async function mainAsync() {
  const args = parseArgs(process.argv);
  const state = {
    channel: args.channel,
    chatId: args.chatId,
    wsClient: null,
    configured: false,
    mock: args.mock,
    bootMessage: args.bootMessage,
    echoActions: args.echoActions,
  };

  for await (const chunk of process.stdin) {
    const lines = String(chunk).split("\n").filter(Boolean);
    for (const raw of lines) {
      let frame;
      try {
        frame = JSON.parse(raw);
      } catch {
        emit(buildLog("invalid json from host", "warning", { raw }));
        continue;
      }
      if (frame.type === "configure") {
        const config = frame.config || {};
        if (state.mock) {
          emit(buildProvisioning({
            mode: "interactive_pairing",
            state: "active",
            pairing_code: config.pairing_code || null,
            config_key: config.config_key || null,
            metadata: {
              callback_token: config.callback_token || null,
              encoding_aes_key: config.encoding_aes_key || null,
            },
          }));
          emit(buildState("configured", { configured: true, mock: true }));
          emit(buildReady(state.channel, { name: "wecom_longconn_bridge", configured: true, mock: true }));
          if (state.bootMessage) {
            emit({
              type: "message",
              version: BRIDGE_PROTOCOL_VERSION,
              message: {
                session_id: `${state.channel}:${state.chatId}`,
                channel: state.channel,
                chat_id: state.chatId,
                content: state.bootMessage,
                is_active: true,
              },
            });
          }
          continue;
        }
        state.configured = Boolean(config.bot_id && config.secret);
        const provisioning = {
          mode: "interactive_pairing",
          state: state.configured ? "active" : "pending",
          pairing_code: config.pairing_code || null,
          config_key: config.config_key || null,
          metadata: {
            callback_token: config.callback_token || null,
            encoding_aes_key: config.encoding_aes_key || null,
          },
        };
        emit(buildProvisioning(provisioning));
        emit(buildState("configuring", { configured: state.configured }));

        if (!state.configured) {
          emit(buildReady(state.channel, { name: "wecom_longconn_bridge", configured: false }));
          continue;
        }

        const sdk = await import("@wecom/aibot-node-sdk");
        const wsClient = new sdk.WSClient({
          botId: config.bot_id,
          secret: config.secret,
          wsUrl: config.websocket_url || undefined,
          logger: {
            debug: (message, ...rest) => emit(buildLog(message, "debug", { rest })),
            info: (message, ...rest) => emit(buildLog(message, "info", { rest })),
            warn: (message, ...rest) => emit(buildLog(message, "warning", { rest })),
            error: (message, ...rest) => emit(buildLog(message, "error", { rest })),
          },
        });
        state.wsClient = wsClient;
        wsClient.on("connected", () => emit(buildState("connected")));
        wsClient.on("authenticated", () => {
          emit(buildReady(state.channel, { name: "wecom_longconn_bridge", configured: true }));
          emit(buildState("authenticated"));
        });
        wsClient.on("disconnected", (reason) => emit(buildState("disconnected", { reason })));
        wsClient.on("reconnecting", (attempt) => emit(buildState("reconnecting", { attempt })));
        wsClient.on("error", (error) => emit(buildLog(error.message, "error")));
        wsClient.on("message", async (frameData) => {
          await parseIncomingFrame(frameData, wsClient, state);
        });
        wsClient.on("event", async (frameData) => {
          await parseIncomingFrame(frameData, wsClient, state);
        });
        wsClient.connect();
        continue;
      }

      if (frame.type === "action") {
        const action = frame.action || {};
        let request;
        try {
          request = await actionToRequest(action, state);
          emit(buildLog("translated action", "debug", { request }));
        } catch (error) {
          emit(buildLog("failed to translate action", "error", { error: String(error) }));
          continue;
        }
        if (state.mock) {
          emit(buildLog(`received action ${action.kind || "unknown"}`));
          if (state.echoActions && action.text) {
            emit({
              type: "message",
              version: BRIDGE_PROTOCOL_VERSION,
              message: {
                session_id: `${state.channel}:${state.chatId}`,
                channel: state.channel,
                chat_id: state.chatId,
                content: `echo: ${action.text}`,
              },
            });
          }
          continue;
        }
        if (!state.wsClient) {
          emit(buildLog("WSClient not configured", "error"));
          continue;
        }
        try {
          await state.wsClient[request.op](...request.args);
        } catch (error) {
          if (isReplyAckError(error, 600039) && request.fallback) {
            emit(buildLog("reply ack device unsupported, retrying proactive fallback", "warning", { op: request.op }));
            try {
              await state.wsClient[request.fallback.op](...request.fallback.args);
              emit(buildLog("proactive fallback sent", "info", { op: request.fallback.op }));
              continue;
            } catch (fallbackError) {
              emit(buildLog("proactive fallback failed", "error", { error: renderError(fallbackError), op: request.fallback.op }));
            }
          }
          if (isReplyAckError(error, 600039)) {
            const fallbackChatId = request.fallback?.args?.[0] || action.conversation?.chat_id || null;
            emit(buildLog("wecom outbound rejected for current session/device", "error", {
              chatId: fallbackChatId,
              routeChannel: action.conversation?.route_channel || null,
              actionKind: action.kind || null,
              contentType: action.content_type || null,
            }));
          }
          emit(buildLog("failed to send action", "error", { error: renderError(error), op: request.op }));
        }
      }
    }
  }
}

mainAsync().catch((error) => {
  emit(buildLog("bridge crashed", "error", { error: String(error) }));
  process.exitCode = 1;
});
