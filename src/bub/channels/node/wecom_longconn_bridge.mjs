import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

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
  if (action.metadata?.wecom_msgtype) return String(action.metadata.wecom_msgtype);
  if (action.content_type === "card") return "template_card";
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

async function actionToRequest(action, runtime) {
  const msgtype = inferWeComMsgtype(action);
  const reqId = action.reply_grant?.token || action.metadata?.wecom_req_id || null;
  const eventType = action.metadata?.wecom_event_type || null;
  const frameHeaders = reqId ? { headers: { req_id: reqId } } : null;
  const text = action.text || "";

  if (eventType === "enter_chat" && frameHeaders) {
    if (msgtype === "template_card" && action.metadata?.template_card) {
      return { mode: "welcome", op: "replyWelcome", args: [frameHeaders, { msgtype: "template_card", template_card: action.metadata.template_card }] };
    }
    return { mode: "welcome", op: "replyWelcome", args: [frameHeaders, { msgtype: "text", text: { content: text } }] };
  }

  if (action.kind === "update_card" && frameHeaders && action.metadata?.template_card) {
    return {
      mode: "event_update",
      op: "updateTemplateCard",
      args: [frameHeaders, action.metadata.template_card, action.metadata?.userids || undefined],
    };
  }

  if (frameHeaders) {
    if (msgtype === "template_card" && action.metadata?.template_card) {
      return { mode: "passive_reply", op: "replyTemplateCard", args: [frameHeaders, action.metadata.template_card] };
    }

    const streamId = action.metadata?.wecom_stream_id || `stream_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    let msgItem;
    if (Array.isArray(action.attachments) && action.attachments.length > 0) {
      const imageAttachment = action.attachments.find((item) => String(item.content_type || "").startsWith("image/"));
      if (imageAttachment?.url?.startsWith("data:")) {
        const encoded = imageAttachment.url.split(",", 2)[1];
        const buffer = Buffer.from(encoded, "base64");
        msgItem = [{
          msgtype: "image",
          image: {
            base64: buffer.toString("base64"),
            md5: (await import("node:crypto")).createHash("md5").update(buffer).digest("hex"),
          },
        }];
      }
    }
    return {
      mode: "passive_reply",
      op: "replyStream",
      args: [frameHeaders, streamId, text, true, msgItem],
    };
  }

  if (msgtype === "template_card" && action.metadata?.template_card) {
    return {
      mode: "proactive_reply",
      op: "sendMessage",
      args: [action.conversation?.chat_id, { msgtype: "template_card", template_card: action.metadata.template_card }],
    };
  }

  const { mentioned_list, mentioned_mobile_list } = buildMentionLists(action.mentions);
  const markdownContent = [text];
  if (mentioned_list.length > 0) markdownContent.push(`\n${mentioned_list.map((item) => `<@${item}>`).join(" ")}`);
  if (mentioned_mobile_list.length > 0) markdownContent.push(`\n${mentioned_mobile_list.join(" ")}`);
  return {
    mode: "proactive_reply",
    op: "sendMessage",
    args: [action.conversation?.chat_id, { msgtype: "markdown", markdown: { content: markdownContent.join("") } }],
  };
}

async function parseIncomingFrame(frame, wsClient, state) {
  const body = frame.body || {};
  const headers = frame.headers || {};
  const reqId = headers.req_id;

  if (frame.cmd === "aibot_event_callback" || body.msgtype === "event") {
    const eventType = body.event?.eventtype || "event";
    const chatId = body.chatid || body.from?.userid || state.chatId;
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
        },
        sender: {
          id: body.from?.userid || "unknown",
          id_kind: "wecom_userid",
        },
        reply_grant: {
          mode: "token",
          token: reqId,
          reply_to_message_id: body.msgid,
        },
        metadata: {
          wecom_req_id: reqId,
          wecom_event_type: eventType,
          wecom_raw_msgtype: "event",
        },
      },
    });
    return;
  }

  const chatId = body.chatid || body.from?.userid || state.chatId;
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
      },
      sender: {
        id: body.from?.userid || "unknown",
        id_kind: "wecom_userid",
      },
      reply_grant: {
        mode: "token",
        token: reqId,
        reply_to_message_id: body.msgid,
      },
      attachments,
      metadata: {
        wecom_req_id: reqId,
        wecom_response_url: body.response_url || null,
        wecom_raw_msgtype: body.msgtype || null,
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
        const request = await actionToRequest(action, state);
        emit(buildLog("translated action", "debug", { request }));
        try {
          await state.wsClient[request.op](...request.args);
        } catch (error) {
          emit(buildLog("failed to send action", "error", { error: String(error), op: request.op }));
        }
      }
    }
  }
}

mainAsync().catch((error) => {
  emit(buildLog("bridge crashed", "error", { error: String(error) }));
  process.exitCode = 1;
});
