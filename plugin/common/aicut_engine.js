/* Talking to the AI Engine from an editor that scripts in JavaScript
 * (36장 9번 AI Engine Connector).
 *
 * The plugin does not analyse anything. 35장: AI Engine은 영상의 "두뇌"이고
 * Plugin은 편집기와 AI를 연결하는 "손"이다. This is the wire between them, and
 * it is the JavaScript twin of `aicut_engine.py` - the same five calls:
 *
 *     check()                     the engine is up, and which profiles it holds
 *     submit(source)              hand it the broadcast the editor has open
 *     job(jobId)                  what it is doing now (26장's progress screen)
 *     episodes(projectId)         what it decided to make
 *     editModel(episodeId, mode)  the Common Edit Model for one of them (37장)
 *
 * ExtendScript has no XMLHttpRequest and no fetch. It has a raw `Socket`, so
 * the HTTP is written out here by hand. The socket itself is passed in as a
 * `transport` - a function that takes the request text and returns the reply -
 * for two reasons: the request building and the reply parsing are then testable
 * under node, which has no ExtendScript Socket, and an editor whose scripting
 * host does have a real HTTP client can hand one over instead.
 */

var aicutEngine = (function () {
    "use strict";

    var DEFAULT_HOST = "127.0.0.1";
    //: The state a run reaches when it finished but the pipeline failed inside it.
    var FAILED_STATE = "FAILED";
    var DEFAULT_PORT = 8765;

    function EngineError(message) {
        this.name = "EngineError";
        this.message = message;
    }
    EngineError.prototype = new Error();

    function fail(message) {
        throw new EngineError(message);
    }

    function jsonEncode(value) {
        if (typeof JSON !== "undefined" && JSON.stringify) {
            return JSON.stringify(value);
        }
        return encodeValue(value);
    }

    /* A JSON writer for the hosts that have no JSON object. Only what the
     * engine is ever sent: strings, numbers, booleans, null, arrays, objects. */
    function encodeValue(value) {
        var parts = [];
        var key, i;
        if (value === null || value === undefined) { return "null"; }
        if (typeof value === "string") { return encodeString(value); }
        if (typeof value === "number") { return isFinite(value) ? String(value) : "null"; }
        if (typeof value === "boolean") { return value ? "true" : "false"; }
        if (value instanceof Array) {
            for (i = 0; i < value.length; i++) { parts.push(encodeValue(value[i])); }
            return "[" + parts.join(",") + "]";
        }
        for (key in value) {
            if (value.hasOwnProperty(key)) {
                parts.push(encodeString(key) + ":" + encodeValue(value[key]));
            }
        }
        return "{" + parts.join(",") + "}";
    }

    function encodeString(text) {
        var out = "";
        var i, ch, code;
        for (i = 0; i < text.length; i++) {
            ch = text.charAt(i);
            code = text.charCodeAt(i);
            if (ch === "\"" || ch === "\\") { out += "\\" + ch; }
            else if (ch === "\n") { out += "\\n"; }
            else if (ch === "\r") { out += "\\r"; }
            else if (ch === "\t") { out += "\\t"; }
            else if (code < 32 || code > 126) {
                // Non-ASCII goes out escaped: a path with Korean in it is
                // ordinary here, and the socket writes bytes, not characters.
                out += "\\u" + ("000" + code.toString(16)).slice(-4);
            } else { out += ch; }
        }
        return "\"" + out + "\"";
    }

    function jsonDecode(text, where) {
        try {
            return (typeof JSON !== "undefined" && JSON.parse)
                ? JSON.parse(text)
                : eval("(" + text + ")");
        } catch (e) {
            fail(where + " did not return JSON");
        }
    }

    /* The bytes of one HTTP/1.0 request. 1.0 rather than 1.1 on purpose: it
     * closes the connection at the end of the reply, which is how a raw socket
     * knows the body has finished without parsing chunked encoding. */
    function buildRequest(method, path, body, options) {
        var host = options.host + ":" + options.port;
        var lines = [method + " " + path + " HTTP/1.0",
                     "Host: " + host,
                     "Accept: application/json",
                     "Connection: close"];
        var payload = "";
        if (body !== null && body !== undefined) {
            payload = jsonEncode(body);
            lines.push("Content-Type: application/json");
            lines.push("Content-Length: " + byteLength(payload));
        }
        if (options.apiKey) {
            lines.push("Authorization: Bearer " + options.apiKey);
        }
        return lines.join("\r\n") + "\r\n\r\n" + payload;
    }

    /* Content-Length counts bytes, and the payload is escaped to ASCII by
     * `encodeString`, so its length in characters is its length in bytes.
     * JSON.stringify does not escape non-ASCII, so those are counted here. */
    function byteLength(text) {
        var bytes = 0;
        var i, code;
        for (i = 0; i < text.length; i++) {
            code = text.charCodeAt(i);
            if (code < 0x80) { bytes += 1; }
            else if (code < 0x800) { bytes += 2; }
            else { bytes += 3; }
        }
        return bytes;
    }

    /* Status line, headers and body out of one HTTP reply. */
    function parseResponse(raw, method, path) {
        var split, head, body, status;
        if (!raw) {
            fail("no reply from the engine for " + method + " " + path
                 + ". Start it with 'aicut ui'.");
        }
        split = raw.indexOf("\r\n\r\n");
        if (split < 0) { split = raw.indexOf("\n\n"); }
        head = split < 0 ? raw : raw.substring(0, split);
        body = split < 0 ? "" : raw.substring(split + (raw.charAt(split) === "\r" ? 4 : 2));
        status = Number(String(head.split(/\r?\n/)[0]).split(" ")[1]);
        if (!(status >= 200 && status < 300)) {
            fail(method + " " + path + " -> HTTP " + (status || "?") + " "
                 + body.substring(0, 400));
        }
        if (!body.replace(/^\s+|\s+$/g, "")) { return {}; }
        return jsonDecode(body, method + " " + path);
    }

    /* Why this job failed, or null if it did not.
     *
     * A run the pipeline itself failed returns rather than raising, so the
     * job's `error` can be empty while its state says FAILED and the reason
     * sits in the report. An adapter reading `error` alone then fell through to
     * "no episodes", which it reported as 16장's 제작 가치 있는 콘텐츠 없음 - a
     * normal ending. That is the one thing a failure must not be mistaken for.
     */
    function failureReason(state) {
        var stated = String((state && state.error) || "");
        var reported;
        if (stated.replace(/^\s+|\s+$/g, "")) { return stated; }
        if (!state || state.state !== FAILED_STATE) { return null; }
        reported = String((state.report && state.report.error) || "");
        return reported.replace(/^\s+|\s+$/g, "") || "the engine did not say why";
    }

    function Engine(options) {
        options = options || {};
        this.host = options.host || DEFAULT_HOST;
        this.port = options.port || DEFAULT_PORT;
        this.apiKey = options.apiKey || "";
        // Set by the .jsx to a Socket-backed function. Without one this object
        // can still build and read messages, which is what the tests use.
        this.transport = options.transport || null;
    }

    Engine.prototype.address = function () {
        return "http://" + this.host + ":" + this.port;
    };

    Engine.prototype.call = function (method, path, body) {
        var request, reply;
        if (!this.transport) {
            fail("this engine has no transport: the editor script must supply one");
        }
        request = buildRequest(method, path, body, this);
        reply = this.transport(request);
        return parseResponse(reply, method, path);
    };

    Engine.prototype.check = function () {
        return this.call("GET", "/api/profiles", null);
    };

    /* Hand the engine the file the editor has open. This is 4장's button: the
     * person pressed it, and from here the engine does 5장's whole list. */
    Engine.prototype.submit = function (sourcePath, options) {
        var body = {source: sourcePath};
        var key;
        if (options) {
            for (key in options) {
                if (options.hasOwnProperty(key)) { body[key] = options[key]; }
            }
        }
        return this.call("POST", "/api/projects", body);
    };

    Engine.prototype.job = function (jobId) {
        return this.call("GET", "/api/jobs/" + String(jobId), null);
    };

    Engine.prototype.episodes = function (projectId) {
        return this.call("GET", "/api/projects/" + String(projectId) + "/episodes", null);
    };

    /* The Common Edit Model for one episode (37장).
     *
     * `mode` is 25장's: new_sequence leaves the operator's own timeline alone,
     * edit_current does not. It is theirs to choose, so it is passed through
     * rather than defaulted silently. */
    Engine.prototype.editModel = function (episodeId, mode) {
        return this.call("POST", "/api/episodes/" + String(episodeId) + "/edit-model",
                         {mode: mode || "new_sequence"});
    };

    return {
        DEFAULT_HOST: DEFAULT_HOST,
        DEFAULT_PORT: DEFAULT_PORT,
        FAILED_STATE: FAILED_STATE,
        EngineError: EngineError,
        Engine: Engine,
        failureReason: failureReason,
        buildRequest: buildRequest,
        parseResponse: parseResponse,
        jsonEncode: jsonEncode,
        byteLength: byteLength
    };
}());

if (typeof module !== "undefined" && module.exports) { module.exports = aicutEngine; }
