/** Continuous tab recording through the supported Codex Browser runtime.
 * Import in its JavaScript session. All actions remain explicit caller actions.
 * Pointer positions are recorded from actual CUA moves for an editorial overlay.
 */
import fs from 'node:fs/promises';
import path from 'node:path';

export async function createRecorder(tab, directory) {
  await fs.mkdir(directory, { recursive: true });
  const cdp = await tab.capabilities.get('cdp');
  const frames = [], pointer = [], chapters = [];
  let cursor, running = false, start, position = { x: 900, y: 500 };
  const elapsed = () => (Date.now() - start) / 1000;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function drain() {
    const batch = await cdp.readEvents({afterSequence:cursor, methods:['Page.screencastFrame'],limit:100});
    cursor = batch.cursor;
    if (batch.truncated) throw Error('Recording event buffer truncated');
    for (const event of batch.events) {
      const frame = event.params;
      const file = `${String(frames.length).padStart(6,'0')}.jpg`;
      await fs.writeFile(path.join(directory,file),Buffer.from(frame.data,'base64'));
      frames.push({file,t:frame.metadata.timestamp - start / 1000,metadata:frame.metadata});
      await cdp.send('Page.screencastFrameAck',{sessionId:frame.sessionId});
    }
  }
  return {
    async record(actions) {
      cursor = (await cdp.readEvents({methods:['Page.screencastFrame']})).cursor;
      const viewport = await tab.playwright.evaluate(() => ({width: innerWidth, height: innerHeight}));
      start = Date.now();
      running = true;
      await cdp.send('Page.startScreencast',{format:'jpeg',quality:85,maxWidth:1280,maxHeight:900,everyNthFrame:1});
      const pump = (async()=>{while(running){await drain();await pause(40);}})();
      try { await actions(); await pause(500); }
      finally {
        running=false;
        await pump;
        await drain();
        await cdp.send('Page.stopScreencast');
        await fs.writeFile(path.join(directory,'recording.json'),JSON.stringify({
          duration:elapsed(),viewport,frames,pointer,chapters,
        },null,2));
      }
      return {frames:frames.length,duration:elapsed(),directory};
    },
    chapter(en,zh){chapters.push({t:elapsed(),en,zh});},
    pause,
    async move(x,y) {
      const origin={...position};
      for(let i=1;i<=16;i++) {
        const u=i/16, ease=u*u*(3-2*u);
        position={x:Math.round(origin.x+(x-origin.x)*ease),y:Math.round(origin.y+(y-origin.y)*ease)};
        await tab.cua.move(position);
        pointer.push({t:elapsed(),...position});
        await pause(30);
      }
    },
    async click() {
      pointer.push({t:elapsed(),...position,click:true});
      await tab.cua.click(position);
      await pause(450);
    },
    async type(text) {
      for(const character of text){await tab.cua.type({text:character});await pause(90);}
      await pause(400);
    },
    async scroll(delta) {
      for(let i=0;i<10;i++) {
        await tab.cua.scroll({...position,scrollY:Math.round(delta/10),scrollX:0});
        await pause(55);
      }
      await pause(400);
    },
  };
}
