/* Premiere Pro Adapter (플러그인 기획안 37장).
 *
 *     AI Engine -> Common Edit Model -> Premiere Adapter -> Premiere Pro
 *
 * This file is the last arrow. It reads the Common Edit Model and calls
 * Premiere; it decides nothing. Every decision - which spans survive, what
 * order they go in, how seconds become frames and ticks - is in
 * `plugin/common/aicut_model.js` and `aicut_build.js`, which have tests that
 * run without Premiere. This is the Resolve adapter's twin: one meaning of the
 * model, two editors, which is what 37장 put the layer there for.
 *
 * Install by copying `plugin/common` and this folder into Premiere's script
 * folder, keeping them side by side:
 *
 *   Windows  %APPDATA%\Adobe\Premiere Pro\<version>\Scripts
 *   macOS    ~/Documents/Adobe/Premiere Pro/<version>/Scripts
 *
 * Then: File > Scripts > aicut_premiere.
 *
 * This is a plain ExtendScript file, not a CEP panel: no ZXP, no signing
 * certificate, no extension manager. Copy the folders and it is installed.
 *
 * Two ways in, and the second is 4장's button:
 *
 *   pick a model file          build from a Common Edit Model already on disk
 *   cancel the dialog          ask the engine about the clip in this sequence,
 *                              have it analyse, then build
 *
 * NOT VERIFIED HERE. Premiere Pro is not present in the environment this was
 * written in, so these API calls are written against Adobe's ExtendScript
 * documentation and have not been executed. The arithmetic they depend on has
 * been, and so has the shape of the engine's HTTP. Treat the first run as the
 * test.
 *
 * Two things to check on that first run, both stated as constants rather than
 * buried in the arithmetic so they are easy to flip:
 *   - `OUT_POINT_IS_EXCLUSIVE` in aicut_build.js. If every clip lands one frame
 *     short, this is why.
 *   - `insertClip` on an empty track appends; if the sequence is not empty the
 *     script refuses rather than dropping clips into someone else's edit.
 */

/*global $, app, File, Folder, Socket, Time, alert */

#include "../common/aicut_model.js"
#include "../common/aicut_engine.js"
#include "aicut_build.js"

(function () {
    "use strict";

    var POLL_MS = 3000;

    function say(message) {
        // A script with no panel has two ways to speak. ESTK gets the console,
        // the person running it from the menu gets the dialog at the end.
        $.writeln(message);
    }

    function fail(message) {
        throw new aicutModel.ModelError(message);
    }

    function modelFileFromDialog() {
        var chosen = File.openDialog(
            "aicut edit model (.json) - cancel to analyse this sequence", "*.json", false
        );
        return chosen ? chosen.fsName : null;
    }

    function readFile(path) {
        var file = new File(path);
        var text;
        if (!file.exists) {
            fail("no edit model at " + path);
        }
        file.encoding = "UTF-8";
        file.open("r");
        text = file.read();
        file.close();
        return text;
    }

    // -- 36장 9번 AI Engine Connector, over ExtendScript's raw socket --------
    function socketTransport(host, port) {
        return function (request) {
            var socket = new Socket();
            var reply = "";
            var chunk;
            if (!socket.open(host + ":" + port, "UTF-8")) {
                throw new aicutEngine.EngineError(
                    "no aicut engine at http://" + host + ":" + port
                    + ". Start it with `aicut ui`."
                );
            }
            socket.write(request);
            while (!socket.eof) {
                chunk = socket.read(65536);
                if (!chunk) { break; }
                reply += chunk;
            }
            socket.close();
            return reply;
        };
    }

    function engineFromEnvironment() {
        var host = (typeof $.getenv === "function" && $.getenv("AICUT_ENGINE_HOST"))
            || aicutEngine.DEFAULT_HOST;
        var port = Number((typeof $.getenv === "function" && $.getenv("AICUT_ENGINE_PORT"))
            || aicutEngine.DEFAULT_PORT);
        var key = (typeof $.getenv === "function" && $.getenv("AICUT_API_KEY")) || "";
        return new aicutEngine.Engine({
            host: host, port: port, apiKey: key,
            transport: socketTransport(host, port)
        });
    }

    // -- 36장 2번 Project Reader / 3번 Media Reader --------------------------
    function currentProject() {
        if (!app.project) { fail("open a project in Premiere first"); }
        return app.project;
    }

    function sequenceFps(sequence) {
        // Premiere gives the sequence's frame duration in ticks; the frame rate
        // is what the model's seconds have to be snapped to. Asking the model
        // instead would put every cut a fraction of a frame late.
        var frameTicks = Number(sequence.timebase);
        if (!(frameTicks > 0)) {
            fail("could not read the sequence's frame rate. Open a sequence first.");
        }
        return aicutBuild.TICKS_PER_SECOND / frameTicks;
    }

    /* The file the operator put on the sequence (4장 step 2-3).
     *
     * 4장 has them import the broadcast and drop it on a sequence before
     * pressing the button, so that clip is what the engine should analyse.
     * Reported rather than guessed at: a sequence holding several different
     * files is not one broadcast, and picking one would analyse something they
     * did not ask about. */
    function sourceInSequence(sequence) {
        var paths = [];
        var t, c, track, clip, path;
        if (!sequence) {
            fail("no sequence is open. 4장: put the broadcast on a sequence first, "
                 + "then press the button.");
        }
        for (t = 0; t < sequence.videoTracks.numTracks; t++) {
            track = sequence.videoTracks[t];
            for (c = 0; c < track.clips.numItems; c++) {
                clip = track.clips[c];
                path = (clip.projectItem && clip.projectItem.getMediaPath)
                    ? String(clip.projectItem.getMediaPath()) : "";
                if (path && indexOf(paths, path) < 0) { paths.push(path); }
            }
        }
        if (!paths.length) {
            fail("this sequence has no video clip with a file behind it");
        }
        if (paths.length > 1) {
            fail("this sequence holds " + paths.length + " different files. Put the one "
                 + "broadcast on a sequence of its own, or run the script with a model file.");
        }
        return paths[0];
    }

    function indexOf(list, wanted) {
        var i;
        for (i = 0; i < list.length; i++) {
            if (list[i] === wanted) { return i; }
        }
        return -1;
    }

    function findOrImport(project, path) {
        var wanted = aicutModel.baseName(path);
        var root = project.rootItem;
        var i, item;
        for (i = 0; i < root.children.numItems; i++) {
            item = root.children[i];
            if (item.name === wanted) { return item; }
        }
        if (!new File(path).exists) {
            fail("the model's source is not at " + path + ". Move it back, or re-run "
                 + "the analysis against its new location.");
        }
        project.importFiles([path], true, project.getInsertionBin(), false);
        for (i = 0; i < root.children.numItems; i++) {
            item = root.children[i];
            if (item.name === wanted) { return item; }
        }
        fail("Premiere would not import " + path);
    }

    function timeAt(seconds) {
        var time = new Time();
        time.ticks = aicutBuild.ticks(seconds);
        return time;
    }

    /* The sequence to build into, following 25장's two modes.
     *
     * new_sequence is the default and leaves the operator's own edit alone.
     * edit_current is only ever taken because the person asked for it by name -
     * it is their timeline, and appending into an edit somebody is working on
     * is not a thing to do quietly. */
    function targetSequence(project, document, sequence, mode) {
        var name = aicutModel.timelineName(document, sequence);
        var active;
        if (mode === "edit_current") {
            active = project.activeSequence;
            if (!active) { fail("edit_current needs a sequence open to edit"); }
            return active;
        }
        if (project.createNewSequence) {
            project.createNewSequence(name, name);
        }
        active = project.activeSequence;
        if (!active) {
            fail("Premiere did not open a sequence to build into. Make an empty "
                 + "sequence at the frame rate you want and run this again.");
        }
        return active;
    }

    // -- 36장 4~8번 the controllers -----------------------------------------
    function buildSequence(project, document, sequence, mode) {
        var target = targetSequence(project, document, sequence, mode);
        var fps = sequenceFps(target);
        var track = target.videoTracks[0];
        var item, entries, i, entry, dropped, placed, lines;

        if (mode !== "edit_current" && track.clips.numItems > 0) {
            // A new sequence that already has clips in it is somebody's edit.
            fail("the sequence's first video track already has clips in it. Make an "
                 + "empty sequence for this model so nothing of yours is moved.");
        }

        item = findOrImport(project, aicutModel.mediaPath(document, "source"));
        entries = aicutBuild.clipList(sequence, fps);

        for (i = 0; i < entries.length; i++) {
            entry = entries[i];
            // in/out live on the project item, and insertClip takes what is set
            // there - so they are set immediately before each insert, not once.
            item.setInPoint(entry.inTicks, 4);      // 4 = video and audio
            item.setOutPoint(entry.outTicks, 4);
            track.insertClip(item, timeAt(entry.timelineSeconds));
        }

        say(aicutBuild.summary(document, sequence, fps));
        dropped = aicutModel.droppedSpans(sequence, fps);
        for (i = 0; i < dropped.length; i++) {
            say("  skipped " + dropped[i][0].toFixed(3) + "-" + dropped[i][1].toFixed(3)
                + "s: shorter than one frame at " + fps + " fps");
        }

        placed = aicutModel.audioClips(sequence);
        if (placed.length) {
            // 24장 puts BGM and 효과음 on their own tracks. Premiere's scripting
            // API has no call that places an arbitrary file at an arbitrary
            // time, so this says what is missing rather than leaving a silent
            // gap the person would find only by listening for it.
            say("  " + placed.length + " audio placement(s) the model asks for are not applied:");
            for (i = 0; i < placed.length; i++) {
                say("    " + placed[i].track + " " + (placed[i].clip.path || "")
                    + " at " + Number(placed[i].clip.timeline_position_sec || 0).toFixed(2) + "s");
            }
        }

        lines = aicutModel.subtitles(sequence);
        if (lines.length) {
            say("  " + lines.length + " caption(s) in the model; import an .srt for them "
                + "(`aicut export <plan> --format srt`)");
        }
        return target;
    }

    function buildFromFile(modelPath, mode) {
        var project = currentProject();
        var document = aicutModel.parse(readFile(modelPath), modelPath);
        var list = aicutModel.sequences(document);
        var built = [];
        var i;
        for (i = 0; i < list.length; i++) {
            built.push(buildSequence(project, document, list[i], document.mode || mode));
        }
        return built;
    }

    /* 4장's button: analyse what is on this sequence, then build from it.
     *
     * The plugin analyses nothing (35장). It hands the engine the file the
     * operator already put on the sequence, waits, and lays out what comes back.
     */
    function buildFromEngine(mode) {
        var project = currentProject();
        var source = sourceInSequence(project.activeSequence);
        var engine = engineFromEnvironment();
        var job, jobId, projectId, state, line, last = "", failed, episodes, document, list;
        var built = [];
        var i, j;

        say("analysing " + aicutModel.baseName(source));
        job = engine.submit(source);
        jobId = job.job_id || job.id;
        projectId = job.project_id || "";
        if (!jobId) {
            throw new aicutEngine.EngineError("the engine did not return a job id");
        }

        // 26장's progress panel is this loop's output. The engine names its own
        // stages; repeating them is the panel, and inventing stage names here
        // would describe a pipeline that is not the one running.
        while (true) {
            state = engine.job(jobId);
            line = state.state || state.status || "";
            if (line && line !== last) {
                say("  " + line);
                last = line;
            }
            if (!state.running) { break; }
            $.sleep(POLL_MS);
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
            say("the engine found nothing worth producing in this broadcast (16장)");
            return built;
        }
        for (i = 0; i < episodes.length; i++) {
            document = aicutModel.validated(
                engine.editModel(episodes[i].episode_id, mode)
            );
            list = aicutModel.sequences(document);
            for (j = 0; j < list.length; j++) {
                built.push(buildSequence(project, document, list[j], mode));
            }
        }
        return built;
    }

    function main() {
        var mode = (typeof $.getenv === "function" && $.getenv("AICUT_TIMELINE_MODE"))
            || "new_sequence";
        var modelPath = (typeof $.getenv === "function" && $.getenv("AICUT_MODEL"))
            || modelFileFromDialog();
        try {
            if (modelPath) {
                buildFromFile(modelPath, mode);
            } else {
                // Cancelling the dialog is how the operator says "just do it" -
                // the button of 4장 - so it falls through to the engine rather
                // than stopping.
                buildFromEngine(mode);
            }
        } catch (e) {
            // A dialog, because a script run from the menu has no console open
            // and a silent failure looks like the plugin did nothing.
            alert("aicut: " + (e.message || e));
            say("aicut: " + (e.message || e));
        }
    }

    main();
}());
