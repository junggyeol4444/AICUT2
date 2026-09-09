/* VEGAS Pro Adapter (플러그인 기획안 37장).
 *
 *     AI Engine -> Common Edit Model -> VEGAS Adapter -> VEGAS Pro
 *
 * This file is the last arrow: it reads the Common Edit Model and calls VEGAS,
 * and it decides nothing. Every decision - which spans survive, what order they
 * go in, how seconds become frames - is in `plugin/common/aicut_model.js` and
 * `aicut_vegas_time.js`, which have tests that run without VEGAS.
 *
 * VEGAS compiles one script file at a time: there is no #include and no
 * require. So the file that goes in the Script Menu is BUILT from the shared
 * modules plus this body - `python plugin/vegas/bundle.py` writes it. That is
 * why this file alone is not the installable script: the alternative was a
 * third copy of what the model means, and two copies of that already disagree
 * more often than anyone expects.
 *
 * NOT VERIFIED IN VEGAS. VEGAS Pro is Windows-only and is not present in the
 * environment this was written in, so these API calls have not been executed.
 * They are written against the VEGAS scripting API, whose class names moved
 * from `Sony.Vegas` to `ScriptPortal.Vegas` at version 14 and whose
 * constructors differ between versions - so each one is tried in both shapes
 * rather than assuming this machine's. Treat the first run as the test.
 */

import System;
import System.IO;
import System.Net;
import System.Text;
import System.Windows.Forms;
import ScriptPortal.Vegas;

var AICUT_POLL_MS = 3000;

function aicutSay(vegas, message) {
    // VEGAS has no console for a script run from the menu, so the transcript
    // goes to the Script log window, which stays open after the run.
    try {
        vegas.DebugOut(message);
    } catch (e) {
        // Older hosts spell it differently; the dialog at the end still says
        // what happened, so a missing log is not a reason to stop.
    }
}

function aicutReadFile(path) {
    if (!File.Exists(path)) {
        throw new aicutModel.ModelError("no edit model at " + path);
    }
    return File.ReadAllText(path, Encoding.UTF8);
}

function aicutModelFromDialog() {
    var dialog = new OpenFileDialog();
    dialog.Title = "aicut edit model (.json) - cancel to analyse a broadcast";
    dialog.Filter = "aicut edit model (*.json)|*.json";
    if (dialog.ShowDialog() == DialogResult.OK) {
        return dialog.FileName;
    }
    return null;
}

function aicutBroadcastFromDialog() {
    var dialog = new OpenFileDialog();
    dialog.Title = "the broadcast to analyse (4장)";
    dialog.Filter = "video|*.mkv;*.mp4;*.mov;*.ts;*.flv|all files|*.*";
    if (dialog.ShowDialog() == DialogResult.OK) {
        return dialog.FileName;
    }
    return null;
}

/* 36장 9번 AI Engine Connector, over .NET's own HTTP client.
 *
 * The shared connector builds a request and reads a reply; WebClient hands back
 * a body and throws for anything that is not 2xx. So the reply is put back
 * together in the shape the connector parses, which keeps one reader of the
 * engine's answers rather than two. */
function aicutTransport(host, port) {
    return function (request) {
        var split = request.indexOf("\r\n\r\n");
        var head = request.substring(0, split).split("\r\n");
        var body = request.substring(split + 4);
        var line = head[0].split(" ");
        var method = line[0];
        var url = "http://" + host + ":" + port + line[1];
        var client = new WebClient();
        var i, header, colon, answer;

        client.Encoding = Encoding.UTF8;
        for (i = 1; i < head.length; i++) {
            colon = head[i].indexOf(":");
            header = head[i].substring(0, colon);
            if (header == "Host" || header == "Content-Length" || header == "Connection") {
                continue;                     // WebClient sets these itself
            }
            client.Headers.Add(header, head[i].substring(colon + 1).replace(/^\s+/, ""));
        }
        try {
            answer = (method == "GET")
                ? client.DownloadString(url)
                : client.UploadString(url, method, body);
            return "HTTP/1.0 200 OK\r\n\r\n" + answer;
        } catch (exc) {
            var response = exc.Response;
            var status = 0;
            var detail = "";
            if (response != null) {
                status = int(response.StatusCode);
                detail = new StreamReader(response.GetResponseStream(), Encoding.UTF8).ReadToEnd();
                return "HTTP/1.0 " + status + " error\r\n\r\n" + detail;
            }
            // No response at all is the engine not being there, which is the
            // common case by a distance.
            throw new aicutEngine.EngineError(
                "no aicut engine at http://" + host + ":" + port
                    + ". Start it with 'aicut ui'."
            );
        }
    };
}

function aicutEngineFrom(host, port, apiKey) {
    return new aicutEngine.Engine({
        host: host || aicutEngine.DEFAULT_HOST,
        port: port || aicutEngine.DEFAULT_PORT,
        apiKey: apiKey || "",
        transport: aicutTransport(host || aicutEngine.DEFAULT_HOST,
                                 port || aicutEngine.DEFAULT_PORT)
    });
}

/* -- 36장 2~3번 Project Reader / Media Reader --------------------------- */
function aicutProjectFps(project) {
    // The project's own rate, not the model's: a timeline built at another rate
    // slides every cut, and the project the adapter is filling is the one that
    // decides.
    var fps = Number(project.Video.FrameRate);
    if (!(fps > 0)) {
        throw new aicutModel.ModelError(
            "could not read the project's frame rate. Set it in Project "
                + "Properties first."
        );
    }
    return fps;
}

function aicutMedia(vegas, path) {
    if (!File.Exists(path)) {
        throw new aicutModel.ModelError(
            "the model's source is not at " + path + ". Move it back, or re-run "
                + "the analysis against its new location."
        );
    }
    return new Media(path);
}

function aicutNewVideoTrack(project, index, name) {
    // The constructor gained a project argument somewhere between versions.
    try {
        return new VideoTrack(project, index, name);
    } catch (e) {
        return new VideoTrack(index, name);
    }
}

function aicutNewAudioTrack(project, index, name) {
    try {
        return new AudioTrack(project, index, name);
    } catch (e) {
        return new AudioTrack(index, name);
    }
}

function aicutTimecode(frames, fps) {
    try {
        return Timecode.FromFrames(frames);
    } catch (e) {
        // Older hosts count from seconds. frames/fps lands on the grid either
        // way, which is the point of doing the arithmetic in frames first.
        return Timecode.FromSeconds(frames / fps);
    }
}

function aicutNewVideoEvent(project, start, length) {
    try {
        return new VideoEvent(project, start, length);
    } catch (e) {
        return new VideoEvent(start, length);
    }
}

function aicutNewAudioEvent(project, start, length) {
    try {
        return new AudioEvent(project, start, length);
    } catch (e) {
        return new AudioEvent(start, length);
    }
}

/* -- 36장 4~8번 the controllers ----------------------------------------- */
function aicutBuildSequence(vegas, document, sequence, mode) {
    var project = vegas.Project;
    var fps = aicutProjectFps(project);
    var events = aicutVegasTime.eventList(sequence, fps);
    var media = aicutMedia(vegas, aicutModel.mediaPath(document, "source"));
    var name = aicutModel.timelineName(document, sequence);
    var videoTrack, audioTrack, videoStream, audioStream;
    var i, entry, videoEvent, audioEvent, take;

    if (mode != "edit_current" && project.Tracks.Count > 0) {
        // 25장 mode A leaves the operator's own edit alone: new tracks beside
        // it, never events dropped into theirs. Appending into an edit somebody
        // is working on is only ever done because they asked for it by name.
        aicutSay(vegas, "this project already has tracks; adding new ones for " + name);
    }

    videoStream = media.GetVideoStreamByIndex(0);
    if (videoStream == null) {
        throw new aicutModel.ModelError(
            "VEGAS sees no video stream in " + aicutModel.baseName(media.FilePath)
        );
    }
    try {
        audioStream = media.GetAudioStreamByIndex(0);
    } catch (e) {
        audioStream = null;
    }

    videoTrack = aicutNewVideoTrack(project, project.Tracks.Count, name);
    project.Tracks.Add(videoTrack);
    if (audioStream != null) {
        // 24장 gives 원본 음성 its own track. VEGAS does not link A and V the
        // way the other two editors do, so the source audio is laid down
        // alongside rather than assumed to come with the picture.
        audioTrack = aicutNewAudioTrack(project, project.Tracks.Count, name + " 원본 음성");
        project.Tracks.Add(audioTrack);
    }

    for (i = 0; i < events.length; i++) {
        entry = events[i];
        videoEvent = aicutNewVideoEvent(
            project,
            aicutTimecode(entry.startFrames, fps),
            aicutTimecode(entry.lengthFrames, fps)
        );
        videoTrack.Events.Add(videoEvent);
        take = videoEvent.AddTake(videoStream);
        take.Offset = aicutTimecode(entry.offsetFrames, fps);

        if (audioStream != null) {
            audioEvent = aicutNewAudioEvent(
                project,
                aicutTimecode(entry.startFrames, fps),
                aicutTimecode(entry.lengthFrames, fps)
            );
            audioTrack.Events.Add(audioEvent);
            take = audioEvent.AddTake(audioStream);
            take.Offset = aicutTimecode(entry.offsetFrames, fps);
        }
    }

    aicutSay(vegas, aicutVegasTime.summary(document, sequence, fps));
    var dropped = aicutModel.droppedSpans(sequence, fps);
    for (i = 0; i < dropped.length; i++) {
        aicutSay(vegas, "  skipped " + dropped[i][0].toFixed(3) + "-"
                 + dropped[i][1].toFixed(3) + "s: shorter than one frame at " + fps + " fps");
    }
    var placed = aicutModel.audioClips(sequence);
    if (placed.length) {
        // 24장 puts BGM and 효과음 on their own tracks. The model states a path
        // and a time; laying them down would mean deciding their level and
        // length here, which is 8.2's to state and not the adapter's to invent.
        aicutSay(vegas, "  " + placed.length + " audio placement(s) the model asks for are not applied:");
        for (i = 0; i < placed.length; i++) {
            aicutSay(vegas, "    " + placed[i].track + " " + (placed[i].clip.path || "")
                     + " at " + Number(placed[i].clip.timeline_position_sec || 0).toFixed(2) + "s");
        }
    }
    var lines = aicutModel.subtitles(sequence);
    if (lines.length) {
        aicutSay(vegas, "  " + lines.length + " caption(s) in the model; import an .srt "
                 + "for them (`aicut export <plan> --format srt`)");
    }
    return videoTrack;
}

function aicutBuildFromFile(vegas, path, mode) {
    var document = aicutModel.parse(aicutReadFile(path), path);
    var list = aicutModel.sequences(document);
    var built = [];
    var i;
    for (i = 0; i < list.length; i++) {
        built.push(aicutBuildSequence(vegas, document, list[i], document.mode || mode));
    }
    return built;
}

/* 4장's button: hand the engine the broadcast, wait, build what comes back.
 *
 * The plugin analyses nothing (35장). VEGAS can say what media is in the
 * project, but the person may have several things open, so the broadcast is
 * picked in a dialog rather than guessed at. */
function aicutBuildFromEngine(vegas, sourcePath, mode, engine) {
    var built = [];
    var state, line, last = "", failed, episodes, document, list, i, j;
    var job, jobId, projectId;

    engine = engine || aicutEngineFrom(null, null, null);
    aicutSay(vegas, "analysing " + aicutModel.baseName(sourcePath));
    job = engine.submit(sourcePath);
    jobId = job.job_id || job.id;
    projectId = job.project_id || "";
    if (!jobId) {
        throw new aicutEngine.EngineError("the engine did not return a job id");
    }

    // 26장's progress panel is this loop's output. The engine names its own
    // stages; repeating them is the panel, and inventing stage names here would
    // describe a pipeline that is not the one running.
    while (true) {
        state = engine.job(jobId);
        line = state.state || state.status || "";
        if (line && line != last) {
            aicutSay(vegas, "  " + line);
            last = line;
        }
        if (!state.running) { break; }
        System.Threading.Thread.Sleep(AICUT_POLL_MS);
    }
    failed = aicutEngine.failureReason(state);
    if (failed) {
        throw new aicutEngine.EngineError("the analysis failed: " + failed);
    }
    projectId = state.project_id || projectId;
    if (!projectId) {
        throw new aicutEngine.EngineError("the engine did not say which project it made");
    }

    episodes = engine.episodes(projectId);
    if (!episodes || !episodes.length) {
        // 16장: 제작 가치 있는 콘텐츠 없음 is a normal ending, not a failure.
        aicutSay(vegas, "the engine found nothing worth producing in this broadcast (16장)");
        return built;
    }
    for (i = 0; i < episodes.length; i++) {
        document = aicutModel.validated(engine.editModel(episodes[i].episode_id, mode));
        list = aicutModel.sequences(document);
        for (j = 0; j < list.length; j++) {
            built.push(aicutBuildSequence(vegas, document, list[j], mode));
        }
    }
    return built;
}

class EntryPoint {
    function FromVegas(vegas : Vegas) {
        var mode = Environment.GetEnvironmentVariable("AICUT_TIMELINE_MODE");
        var modelPath = Environment.GetEnvironmentVariable("AICUT_MODEL");
        var host = Environment.GetEnvironmentVariable("AICUT_ENGINE_HOST");
        var portText = Environment.GetEnvironmentVariable("AICUT_ENGINE_PORT");
        var apiKey = Environment.GetEnvironmentVariable("AICUT_API_KEY");
        var broadcast;
        if (!mode) { mode = "new_sequence"; }
        try {
            if (!modelPath) { modelPath = aicutModelFromDialog(); }
            if (modelPath) {
                aicutBuildFromFile(vegas, modelPath, mode);
            } else {
                // Cancelling the model dialog is how the operator says "just do
                // it" - the button of 4장 - so it asks which broadcast instead
                // of stopping.
                broadcast = aicutBroadcastFromDialog();
                if (broadcast) {
                    aicutBuildFromEngine(
                        vegas, broadcast, mode,
                        aicutEngineFrom(host, portText ? Number(portText) : 0, apiKey)
                    );
                }
            }
        } catch (e) {
            // A dialog, because a script run from the menu has no console open
            // and a silent failure looks like the plugin did nothing.
            MessageBox.Show("aicut: " + (e.message || e), "aicut");
            aicutSay(vegas, "aicut: " + (e.message || e));
        }
    }
}
