/*
 * 10 Minute Mail -- browser client.
 *
 * Plain JavaScript, no framework and no build step. The whole UI is one
 * address, one timer and a list; React would mean npm, a bundler and a
 * few hundred megabytes of node_modules to render it, and would hide
 * the WebSocket handling that is the interesting part.
 *
 * Structure mirrors the backend: small objects with one job each, wired
 * together at the bottom.
 *
 *   Api        -- talks HTTP to the server
 *   Storage    -- remembers the address across refreshes
 *   Countdown  -- ticks the timer down
 *   Inbox      -- renders messages
 *   LiveFeed   -- the WebSocket, with reconnection
 *   App        -- wires them together and owns the buttons
 */

"use strict";

/* ------------------------------------------------------------------ */
/* Elements                                                            */
/* ------------------------------------------------------------------ */

const elements = {
  address: document.getElementById("address"),
  copyButton: document.getElementById("copy-button"),
  countdown: document.getElementById("countdown"),
  extendButton: document.getElementById("extend-button"),
  inbox: document.getElementById("inbox"),
  status: document.getElementById("status"),
};

/* ------------------------------------------------------------------ */
/* Status line                                                         */
/* ------------------------------------------------------------------ */

let statusTimer = null;

/**
 * Show a transient message under the address field.
 *
 * @param {string} text
 * @param {"info"|"success"|"error"} kind
 */
function setStatus(text, kind = "info") {
  clearTimeout(statusTimer);
  elements.status.textContent = text;
  elements.status.className = `status ${kind === "info" ? "" : kind}`;

  // Errors stay put; confirmations get out of the way on their own.
  if (kind !== "error" && text) {
    statusTimer = setTimeout(() => {
      elements.status.textContent = "";
      elements.status.className = "status";
    }, 3000);
  }
}

/* ------------------------------------------------------------------ */
/* Api -- every HTTP call in one place                                 */
/* ------------------------------------------------------------------ */

const Api = {
  async createMailbox() {
    const response = await fetch("/api/mailboxes", { method: "POST" });
    if (!response.ok) throw new Error(`create failed: ${response.status}`);
    return response.json();
  },

  /**
   * Fetch a mailbox's status.
   * Returns null when the mailbox is gone (404) or expired (410) --
   * both mean "you need a new one", which the caller handles the same
   * way, so they collapse into one sentinel rather than two throws.
   */
  async getMailbox(address) {
    const response = await fetch(`/api/mailboxes/${encodeURIComponent(address)}`);
    if (response.status === 404 || response.status === 410) return null;
    if (!response.ok) throw new Error(`lookup failed: ${response.status}`);
    return response.json();
  },

  async extendMailbox(address) {
    const response = await fetch(
      `/api/mailboxes/${encodeURIComponent(address)}/extend`,
      { method: "POST" }
    );
    if (!response.ok) throw new Error(`extend failed: ${response.status}`);
    return response.json();
  },

  async listMessages(address) {
    const response = await fetch(
      `/api/mailboxes/${encodeURIComponent(address)}/messages`
    );
    if (!response.ok) throw new Error(`inbox failed: ${response.status}`);
    return response.json();
  },
};

/* ------------------------------------------------------------------ */
/* Storage -- survive a page refresh                                   */
/* ------------------------------------------------------------------ */

const Storage = {
  KEY: "ten-min-mail.address",

  /*
   * Every access is wrapped: localStorage throws in Safari private
   * browsing and when a user has disabled site data. Losing the
   * remembered address is a minor inconvenience; an exception here
   * would stop the page loading at all.
   */
  read() {
    try {
      return localStorage.getItem(this.KEY);
    } catch {
      return null;
    }
  },

  write(address) {
    try {
      localStorage.setItem(this.KEY, address);
    } catch {
      /* not fatal */
    }
  },

  clear() {
    try {
      localStorage.removeItem(this.KEY);
    } catch {
      /* not fatal */
    }
  },
};

/* ------------------------------------------------------------------ */
/* Clipboard                                                           */
/* ------------------------------------------------------------------ */

/**
 * Copy text, with a fallback for insecure contexts.
 *
 * navigator.clipboard only exists in a secure context: HTTPS, or
 * localhost. Opened over plain HTTP on a LAN address -- exactly what
 * happens when you run this in Docker and visit it from your phone --
 * it is undefined, and the button would silently do nothing.
 *
 * So we try the modern API and fall back to the old execCommand
 * technique. It is deprecated, but it works where the good API is not
 * available, and a deprecated path that works beats a modern one that
 * silently fails.
 *
 * @returns {Promise<boolean>} whether the copy succeeded
 */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through: permission denied, or the page is not focused.
    }
  }

  // Legacy path: put the text in an off-screen textarea, select it,
  // and ask the document to copy the selection.
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.setAttribute("readonly", "");
  scratch.style.position = "fixed";
  scratch.style.top = "-1000px";
  document.body.appendChild(scratch);

  try {
    scratch.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(scratch);
  }
}

/* ------------------------------------------------------------------ */
/* Countdown                                                           */
/* ------------------------------------------------------------------ */

const Countdown = {
  remaining: 0,
  timer: null,
  onExpired: null,

  start(seconds) {
    this.remaining = seconds;
    this.render();

    clearInterval(this.timer);
    this.timer = setInterval(() => {
      this.remaining -= 1;
      this.render();
      if (this.remaining <= 0) {
        this.stop();
        if (this.onExpired) this.onExpired();
      }
    }, 1000);
  },

  stop() {
    clearInterval(this.timer);
    this.timer = null;
  },

  render() {
    const left = Math.max(0, this.remaining);
    const minutes = String(Math.floor(left / 60)).padStart(2, "0");
    const seconds = String(left % 60).padStart(2, "0");
    elements.countdown.textContent = `${minutes}:${seconds}`;

    // Escalating urgency, so the user notices before it is too late.
    elements.countdown.className = "countdown";
    if (left <= 30) elements.countdown.classList.add("critical");
    else if (left <= 120) elements.countdown.classList.add("warning");
  },
};

/* ------------------------------------------------------------------ */
/* Inbox                                                               */
/* ------------------------------------------------------------------ */

const Inbox = {
  count: 0,

  clear() {
    this.count = 0;
    elements.inbox.innerHTML =
      '<li class="empty">No messages yet. They appear here instantly.</li>';
  },

  /**
   * Add a message to the top of the list.
   *
   * Every field is inserted with textContent, never innerHTML. The
   * sender, subject and body all come from whoever sent the mail, so
   * building this markup by string concatenation would be a stored
   * cross-site-scripting hole. textContent makes the browser treat the
   * value as text, not markup -- the backend already strips HTML, and
   * this is the second, independent line of defence.
   */
  add(message) {
    if (this.count === 0) elements.inbox.innerHTML = "";
    this.count += 1;

    const item = document.createElement("li");
    item.className = "message arriving";

    const head = document.createElement("div");
    head.className = "message-head";

    const sender = document.createElement("span");
    sender.className = "message-sender";
    sender.textContent = message.sender || "(unknown sender)";

    const time = document.createElement("span");
    time.textContent = formatTime(message.received_at);

    head.append(sender, time);

    const subject = document.createElement("p");
    subject.className = "message-subject";
    subject.textContent = message.subject || "(no subject)";

    const body = document.createElement("p");
    body.className = "message-body";
    body.textContent = message.body || "(empty message)";

    item.append(head, subject, body);
    elements.inbox.prepend(item);
  },
};

function formatTime(isoString) {
  try {
    return new Date(isoString).toLocaleTimeString();
  } catch {
    return "";
  }
}

/* ------------------------------------------------------------------ */
/* LiveFeed -- the WebSocket                                           */
/* ------------------------------------------------------------------ */

const LiveFeed = {
  socket: null,
  address: null,
  attempts: 0,
  closedDeliberately: false,
  onMessage: null,

  connect(address) {
    this.address = address;
    this.closedDeliberately = false;

    // ws:// on http, wss:// on https -- mixing them is blocked by the
    // browser, and hardcoding either breaks one of the two deployments.
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${scheme}//${location.host}/ws/${encodeURIComponent(address)}`;

    this.socket = new WebSocket(url);

    this.socket.onopen = () => {
      this.attempts = 0; // a successful connection resets the backoff
    };

    this.socket.onmessage = (event) => {
      const frame = JSON.parse(event.data);
      if (frame.type === "message" && this.onMessage) {
        this.onMessage(frame);
      }
    };

    this.socket.onclose = () => {
      if (!this.closedDeliberately) this.scheduleReconnect();
    };

    // onerror fires before onclose; letting onclose own reconnection
    // keeps the retry logic in one place.
    this.socket.onerror = () => {};
  },

  /*
   * Exponential backoff, capped.
   *
   * Reconnecting immediately in a loop turns a brief server hiccup
   * into a denial of service by every open tab at once. Doubling the
   * delay backs off gracefully; the cap keeps a laptop that woke from
   * sleep from waiting minutes to recover.
   */
  scheduleReconnect() {
    this.attempts += 1;
    if (this.attempts > 8) {
      setStatus("Connection lost. Refresh to reconnect.", "error");
      return;
    }

    const delay = Math.min(1000 * 2 ** (this.attempts - 1), 15000);
    setStatus(`Reconnecting in ${Math.round(delay / 1000)}s…`);
    setTimeout(() => this.connect(this.address), delay);
  },

  disconnect() {
    this.closedDeliberately = true;
    if (this.socket) this.socket.close();
    this.socket = null;
  },
};

/* ------------------------------------------------------------------ */
/* App                                                                 */
/* ------------------------------------------------------------------ */

const App = {
  address: null,

  async start() {
    Countdown.onExpired = () => this.handleExpiry();
    LiveFeed.onMessage = (frame) => Inbox.add(frame);

    elements.copyButton.addEventListener("click", () => this.copyAddress());
    elements.extendButton.addEventListener("click", () => this.extend());

    await this.resumeOrCreate();
  },

  /**
   * Reuse the remembered mailbox if it is still alive, else make one.
   *
   * A refresh should not throw away an address that still has minutes
   * left on it -- someone may already have sent mail to it.
   */
  async resumeOrCreate() {
    const remembered = Storage.read();

    if (remembered) {
      try {
        const mailbox = await Api.getMailbox(remembered);
        if (mailbox) {
          await this.adopt(mailbox, { loadHistory: true });
          return;
        }
      } catch {
        // Fall through to creating a fresh one.
      }
      Storage.clear();
    }

    await this.create();
  },

  async create() {
    try {
      setStatus("Creating a mailbox…");
      const mailbox = await Api.createMailbox();
      Inbox.clear();
      await this.adopt(mailbox, { loadHistory: false });
      setStatus("");
    } catch {
      setStatus("Could not create a mailbox. Is the server running?", "error");
    }
  },

  /** Point the whole UI at a mailbox. */
  async adopt(mailbox, { loadHistory }) {
    this.address = mailbox.address;
    Storage.write(mailbox.address);

    elements.address.value = mailbox.address;
    elements.extendButton.disabled = false;

    Countdown.start(mailbox.remaining_seconds);

    if (loadHistory) {
      // A resumed mailbox may already hold mail that arrived while the
      // page was closed. The WebSocket only carries what comes next.
      Inbox.clear();
      try {
        const messages = await Api.listMessages(mailbox.address);
        messages.forEach((message) => Inbox.add(message));
      } catch {
        /* an empty inbox is an acceptable degradation */
      }
    }

    LiveFeed.disconnect();
    LiveFeed.connect(mailbox.address);
  },

  async copyAddress() {
    if (!this.address) return;

    const copied = await copyText(this.address);
    if (copied) {
      setStatus("Address copied to clipboard.", "success");
    } else {
      // Select the text so the user can copy it by hand rather than
      // being told "it failed" with no way forward.
      elements.address.select();
      setStatus("Press Ctrl+C to copy the selected address.", "error");
    }
  },

  async extend() {
    if (!this.address) return;

    elements.extendButton.disabled = true;
    try {
      const mailbox = await Api.extendMailbox(this.address);
      Countdown.start(mailbox.remaining_seconds);
      setStatus("Reset to a full ten minutes.", "success");
    } catch {
      setStatus("Could not extend. The mailbox may have expired.", "error");
      await this.handleExpiry();
    } finally {
      elements.extendButton.disabled = false;
    }
  },

  async handleExpiry() {
    LiveFeed.disconnect();
    Storage.clear();
    elements.extendButton.disabled = true;
    setStatus("This mailbox expired. Creating a new one…", "error");

    // Brief pause so the message is readable before the UI resets.
    setTimeout(() => this.create(), 1500);
  },
};

App.start();
