/* The VEGAS Pro half of the arithmetic: frames on the project's own clock.
 *
 *     AI Engine -> Common Edit Model -> VEGAS Adapter -> VEGAS Pro
 *
 * `plugin/common/aicut_model.js` answers what the model means; this file turns
 * that answer into the three numbers a VEGAS event needs, and nothing here
 * touches VEGAS, so it runs under plain node - which is how it is tested on a
 * machine with no VEGAS install on it. `aicut_vegas_body.js` is the part that
 * talks to the application.
 *
 * VEGAS states an event as a start and a length on the timeline, plus the take's
 * offset into the source. There is no end frame at all, so the off-by-one that
 * Resolve and Premiere have to declare does not arise here - a length of 30
 * frames is 30 frames in both directions.
 *
 * ES3 on purpose: VEGAS compiles its .js scripts as JScript.NET, which is not
 * modern JavaScript either.
 */

/*global aicutModel */

var aicutVegasTime = (function (model) {
    "use strict";

    /* Every event to place, in timeline order.
     *
     * Timeline order is the model's, not source order: 2.4 lets a video open on
     * the moment that happened last, and sorting by source would quietly undo
     * the structure the engine chose.
     *
     * Everything is in frames on the project's own clock. VEGAS will happily
     * take seconds and put an event between two frames, and a timeline full of
     * sub-frame offsets is a drift nobody can find by the end of it. */
    function eventList(sequence, fps) {
        var ranges = model.frameRanges(sequence, fps, false);
        var events = [];
        var timeline = 0;
        var i;
        for (i = 0; i < ranges.length; i++) {
            events.push({
                startFrames: timeline,
                lengthFrames: ranges[i].frames,
                offsetFrames: ranges[i].startFrame,
                seconds: ranges[i].frames / fps,
                name: ranges[i].clip.name || "",
                role: ranges[i].clip.role || ""
            });
            timeline += ranges[i].frames;
        }
        return events;
    }

    /* How long the finished timeline is, in frames. */
    function totalFrames(sequence, fps) {
        var events = eventList(sequence, fps);
        var last = events[events.length - 1];
        return last.startFrames + last.lengthFrames;
    }

    function summary(document, sequence, fps) {
        return model.summary(document, sequence, fps, false);
    }

    return {
        eventList: eventList,
        totalFrames: totalFrames,
        summary: summary
    };
}(typeof aicutModel !== "undefined"
    ? aicutModel
    : require("../common/aicut_model.js")));

if (typeof module !== "undefined" && module.exports) { module.exports = aicutVegasTime; }
