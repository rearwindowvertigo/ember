# Changelog

## [0.3.0](https://github.com/rearwindowvertigo/ember/compare/v0.2.1...v0.3.0) (2026-11-03)


### ⚠ BREAKING CHANGES

* **merge:** `emb merge` without `--note` exits 64.

### Added

* add emb prune to remove old ended records ([8456be8](https://github.com/rearwindowvertigo/ember/commit/8456be88dd20ebacad0143ee957b1d211bd28d8b))
* add emb prune to remove old ended records (turn 2) ([d54681b](https://github.com/rearwindowvertigo/ember/commit/d54681bdeee9ca789d0d42d12a5e8f0d9d0a09da))
* add emb prune to remove old ended records (turn 3) ([82dbf2d](https://github.com/rearwindowvertigo/ember/commit/82dbf2d43156368bb948e04f7f2e0c413b807a17))
* add emb prune to remove old ended records (turn 4) ([101a6a0](https://github.com/rearwindowvertigo/ember/commit/101a6a07c9bf23ee4e2c74745224fcb316d5546e))
* **app:** count flows and panels waiting for the lead ([caaa771](https://github.com/rearwindowvertigo/ember/commit/caaa771ace25ef8abee7ec691662728c91007e72))
* **app:** filter Finished records and clear them with emb prune ([c0ccf1f](https://github.com/rearwindowvertigo/ember/commit/c0ccf1f39c7befe5b4c5da334657d8a076a18375))
* **app:** open a flow's summary, briefs and check logs ([5cfa427](https://github.com/rearwindowvertigo/ember/commit/5cfa4272f84b698f58ac8517dee66bd17f80b338))
* **app:** show a newer Ember release on the Dependencies screen ([bcbea5e](https://github.com/rearwindowvertigo/ember/commit/bcbea5e17f448db775fd12326e49720ed05a19ee))
* **app:** show running flows and panels in the tray ([2120cd4](https://github.com/rearwindowvertigo/ember/commit/2120cd40d09b98335c2914365a03051e765a7c07))
* **app:** show runs, flows and panels in Live and Finished (turn 2) ([600fbe4](https://github.com/rearwindowvertigo/ember/commit/600fbe43eae99b8f01edfc97fd6cacebcec59945))
* **deps:** list the ember skill link as a component ([acf8b07](https://github.com/rearwindowvertigo/ember/commit/acf8b0739c8369d720d22b3a992d863dc94f22f2))
* **flows:** add emb flow --cancel ([869514e](https://github.com/rearwindowvertigo/ember/commit/869514ea012a2ced558acad6951adfff18ce71fe))
* **merge:** require the verdict line as emb merge --note ([360c53e](https://github.com/rearwindowvertigo/ember/commit/360c53ea230586f12eb750f263a016aa07821d44))
* **panels:** retry a failed member and close a panel ([2fc3cda](https://github.com/rearwindowvertigo/ember/commit/2fc3cda8b9ff4c9a67ea788e5f3f68f4b7988baf))
* **prune:** list leftover worktrees in emb prune --json ([933eaaa](https://github.com/rearwindowvertigo/ember/commit/933eaaa1742871a1c5aa57fe89dca007a556ec8f))


### Fixed

* **app:** act on a click in a tray menu that went stale ([9007dce](https://github.com/rearwindowvertigo/ember/commit/9007dce4e0cc4958f1cf8d948c3f96d437e43974))
* **app:** act on a click in a tray menu that went stale (turn 2) ([8f913ee](https://github.com/rearwindowvertigo/ember/commit/8f913ee50fea57923796bf3caacaa8a99ddfc340))
* **app:** fit the Run this command? dialog to its content ([6f2977d](https://github.com/rearwindowvertigo/ember/commit/6f2977dc485144c3155338aa0e4bfcb6afb715da))
* **app:** fit the Run this command? dialog to its content (turn 2) ([cd86de8](https://github.com/rearwindowvertigo/ember/commit/cd86de8f45ac055663bdab1bae423816c16d87c9))
* **app:** keep nested rows in place and fix final review findings ([22015c3](https://github.com/rearwindowvertigo/ember/commit/22015c36868791a47da96e15aeb93377fba53d27))
* **app:** size the model list's name column to its widest name ([219097a](https://github.com/rearwindowvertigo/ember/commit/219097afe7d5d482d1bbab2e582dfc9e76940acf))
* **choices:** raise the generation only when settings change ([c17189a](https://github.com/rearwindowvertigo/ember/commit/c17189aae1c1e10a2e449c3dea63a49b68ecc0b9))
* **deps:** build update commands from each tool's install method ([dda0d9a](https://github.com/rearwindowvertigo/ember/commit/dda0d9a6b59294a78cbbef02821271ac6481fccd))
* **deps:** build update commands from each tool's install method (turn 2) ([e0da421](https://github.com/rearwindowvertigo/ember/commit/e0da421e274ee0b922dd3a9dafa1a5bf05189c9b))
* **deps:** build update commands from each tool's install method (turn 3) ([e8f0d43](https://github.com/rearwindowvertigo/ember/commit/e8f0d43f9ee14d38ca975b1631a7a9659bc4fc68))
* **flows:** report a cancel without a reason or with a missing run record ([b7604b7](https://github.com/rearwindowvertigo/ember/commit/b7604b78f22f3971fd1ffb6074b300f130fda214))
* **flows:** stop waiting for a stuck runner after a cancel deadline ([34bce25](https://github.com/rearwindowvertigo/ember/commit/34bce255b0415b46337f93b64daef0bbe26077e7))
* **merge:** run merge and cleanup git commands from the main checkout ([0c5b08a](https://github.com/rearwindowvertigo/ember/commit/0c5b08a3f8e12eb9f1b2682993eef52692c114b7))
* **prune:** check branches from a checkout that still exists ([da279b2](https://github.com/rearwindowvertigo/ember/commit/da279b2a936137e22c1550cbcb8c770b7690dd3d))
* **skill:** leave a worker's worktree untouched during checks ([459cc48](https://github.com/rearwindowvertigo/ember/commit/459cc4874e06fa18ed6f91fcdea4a1a565959464))

## [0.2.1](https://github.com/rearwindowvertigo/ember/compare/v0.2.0...v0.2.1) (2026-11-02)


### Fixed

* **app:** give each settings save its own result callback ([6b8e4a2](https://github.com/rearwindowvertigo/ember/commit/6b8e4a27063310b016951cf1791f2bea2afceffb))
* **app:** highlight only the shown part of an elided selection ([73e0a02](https://github.com/rearwindowvertigo/ember/commit/73e0a0215e51da07fa183f21c231c9fe3cfd54f6))
* **app:** pass the tray click's activation token to the terminal ([f1c1b44](https://github.com/rearwindowvertigo/ember/commit/f1c1b449ad5874f622ec7a52dec95c90c3e73b80))
* **app:** show the plan wave on Overview's flow line ([ebb1e96](https://github.com/rearwindowvertigo/ember/commit/ebb1e967be8ca37f2512777f8f428d9f9bdeb8c7))
* **app:** straighten the Engines screen's model rows and cards ([e476595](https://github.com/rearwindowvertigo/ember/commit/e4765957c15f1021691de9da4d22c47427e9ce17))
* **codex:** treat a model at capacity as a provider error ([723403e](https://github.com/rearwindowvertigo/ember/commit/723403e8d51f3e9ecb1fd13d823e7691e23f5c4d))
* **flows:** leave next: and owner: lines out of a stop reason ([e5989f8](https://github.com/rearwindowvertigo/ember/commit/e5989f8b1c2da7df6e202c45db2d6d5640bc06a7))
* **flows:** pass a hook's refusal to the writer with a send answer ([ed1e3d9](https://github.com/rearwindowvertigo/ember/commit/ed1e3d9f712bfed531630350bb2f9c5b19da1d96))
* **skill:** keep verdicts visible and leave kept worktrees alone ([feb143e](https://github.com/rearwindowvertigo/ember/commit/feb143e9a587aaf894847ce75e2e9008e2a7778d))

## [0.2.0](https://github.com/rearwindowvertigo/ember/compare/v0.1.0...v0.2.0) (2026-11-02)


### Added

* **app:** add multi-task work and panel members to the Menu ([5c815a3](https://github.com/rearwindowvertigo/ember/commit/5c815a3ff735115d6e6604f6d8c7d6aaeaf7c919))
* **app:** show flows and panels on Overview with their tasks ([48d20ee](https://github.com/rearwindowvertigo/ember/commit/48d20ee316aa823eff24928f88de8f474a92d670))
* emb flow (build step 6b) ([d4690b8](https://github.com/rearwindowvertigo/ember/commit/d4690b8c478683a3d3a3aa7912bbb68e3020b97c))
* emb gapcheck (build step 6a) ([aaaa70f](https://github.com/rearwindowvertigo/ember/commit/aaaa70fb39443a52e68fa34968d07e2c1ff7c140))
* emb panel (build step 6c) ([96caec3](https://github.com/rearwindowvertigo/ember/commit/96caec38a5cd450ac0ecf0e76f248c8c2e9e5035))
* flows and panels in the app and the skill (build step 6d) ([8282a7a](https://github.com/rearwindowvertigo/ember/commit/8282a7aef6ab08d0cc12f63a39ba2ae8733afd93))
* **flows:** add emb flow to plan, start, watch and answer flows ([d36d886](https://github.com/rearwindowvertigo/ember/commit/d36d8862ea9abc51df7e68fe71c73be2e93e07ea))
* **flows:** add flow reviews, the runner lock and flow branches ([1a84d21](https://github.com/rearwindowvertigo/ember/commit/1a84d210d4921a867600bb7689608bf8d3a104fa))
* **flows:** load and validate flow plans and keep flow records ([b329322](https://github.com/rearwindowvertigo/ember/commit/b3293220739cd53e02be17a1f40d382133f520e5))
* **flows:** run flows in waves with reviews, fix rounds and merges ([3166606](https://github.com/rearwindowvertigo/ember/commit/31666068df8a86953714141ecf19046052b8a8e9))
* **gapcheck:** add emb gapcheck ([83380c9](https://github.com/rearwindowvertigo/ember/commit/83380c9c0a30dc47846a58e6c625359761cb7a8d))
* **gapcheck:** split plans into steps, write the gap check brief and read replies ([f6477bb](https://github.com/rearwindowvertigo/ember/commit/f6477bb7f269fe2251962d84fcf3679ed475e5ec))
* **panels:** add emb panel to ask members and recover late answers ([365ca65](https://github.com/rearwindowvertigo/ember/commit/365ca6565f4b83059c2f3272d58e2aba30b35dd0))
* **panels:** add panel members and the panel.members menu key ([28513a0](https://github.com/rearwindowvertigo/ember/commit/28513a0dd46dbe291c274401a5a00a0fb5c229dd))
* **panels:** run panel members in parallel and write the summary ([aab6cad](https://github.com/rearwindowvertigo/ember/commit/aab6cad6a3d5bc3dcab3650c8130ebab13fdb827))
* **skill:** add the gap check, flow and panel references to Ember ([fbedef5](https://github.com/rearwindowvertigo/ember/commit/fbedef5293a84ebf532e284ae407092e1b4bbc86))


### Fixed

* **app:** unlock Panel members after a stale save whose reload failed ([3fc7c2a](https://github.com/rearwindowvertigo/ember/commit/3fc7c2a406e68237c97410921299eae464b74d9e))
* **app:** watch only run folders for the runs list ([4a03238](https://github.com/rearwindowvertigo/ember/commit/4a03238eed3ec1a970b790be3e579f86ec99b514))
* **cli:** end every usage error with a next line ([5e6134b](https://github.com/rearwindowvertigo/ember/commit/5e6134b4e031b3be898d564f4c7bdaa0152122c0))
* **flows:** review a moved branch afresh when recovering an explicit review ([3362352](https://github.com/rearwindowvertigo/ember/commit/336235274f623ef8003cf9d27f2157ece055ce7f))
* **skill:** merge a flow kept with a fix and keep failure models to the request ([0ea7839](https://github.com/rearwindowvertigo/ember/commit/0ea7839710e2440f52707d019207c4d2730ffe57))
* **skill:** run emb merge as its own command after the verdict line ([4710822](https://github.com/rearwindowvertigo/ember/commit/47108222aceed4c50c5bf226173bdb1f6a88f758))
* **skill:** state verdicts before merging and keep briefs to the request ([25de01d](https://github.com/rearwindowvertigo/ember/commit/25de01d2deaea48270b97a7b3371ec8f5e40f6fb))
* **skill:** visible verdicts and member lines, and eval stubs that follow the plan ([54861f2](https://github.com/rearwindowvertigo/ember/commit/54861f298eb3d8d301ca02eaf5efc0cbebd958e8))
* **skill:** write the flow verdict before its merge and keep a Discard unmerged ([78496e1](https://github.com/rearwindowvertigo/ember/commit/78496e1c8dc428b2c58211556b478d89a29cbc0b))
* **skill:** write the menu and verdict lines right before their commands ([39e75d5](https://github.com/rearwindowvertigo/ember/commit/39e75d5dbe596e3d5e27b319e77c62dc59f7c327))

## 0.1.0 (2026-11-02)


### Added

* add the ember skill and the .deb package ([abf7850](https://github.com/rearwindowvertigo/ember/commit/abf78504256ce7ebfebdfd35549d5a795269bdf2))
* **app:** add emb app with the window, tray, run watcher and terminals ([4891a1c](https://github.com/rearwindowvertigo/ember/commit/4891a1c1f20bd2591d661c95c224be0ba632c765))
* **app:** add PySide6, theme, widgets, icons and assets ([56ea85b](https://github.com/rearwindowvertigo/ember/commit/56ea85b80008945e91a385f20fcf34a057670ba2))
* **app:** add support modules and drop notification settings ([e5b2216](https://github.com/rearwindowvertigo/ember/commit/e5b22166342333b15f514f3cde7f50c521596e84))
* **app:** add the Dependencies screen ([8ab612c](https://github.com/rearwindowvertigo/ember/commit/8ab612c58682c32647a1cfe310dd829c935f88be))
* **app:** add the Engines screen ([c4577d6](https://github.com/rearwindowvertigo/ember/commit/c4577d613d6025d3ba2445ae55db25969983ee6f))
* **app:** add the Menu and Settings screens ([b19bd9f](https://github.com/rearwindowvertigo/ember/commit/b19bd9f2d4285b7c53f14d054e18ead486606d84))
* **app:** add the Overview screen and the screen mirror ([e57cb38](https://github.com/rearwindowvertigo/ember/commit/e57cb388332dc7340c0bf6ad174125bd08603f98))
* **app:** report a runs folder read failure on Overview ([fa37f8e](https://github.com/rearwindowvertigo/ember/commit/fa37f8eb77976a01018fd958c69ece3b0de59ade))
* **app:** scroll the screen mirror with the keyboard ([1aefa90](https://github.com/rearwindowvertigo/ember/commit/1aefa90c06b648651f2d7fdcc62317bf5c9d89b4))
* **branches:** add emb merge and emb cleanup ([cc91bc5](https://github.com/rearwindowvertigo/ember/commit/cc91bc50856230071a391ec58871651fa96d1c69))
* build step 1: tooling, paths, settings and choices ([#1](https://github.com/rearwindowvertigo/ember/issues/1)) ([211e290](https://github.com/rearwindowvertigo/ember/commit/211e2900c41bf91dba6d5656284abcfdf2cf4e6a))
* **choices:** add settings store, one-time choices, claims and emb choices ([d356e58](https://github.com/rearwindowvertigo/ember/commit/d356e586ffcde00d517f72e50ca1e1f3a827c0e8))
* dependency checks and the desktop app (build step 4) ([166e492](https://github.com/rearwindowvertigo/ember/commit/166e49283d44d5a1cd8325cda214bcb77ab316a9))
* **deps:** add dependency checks and emb deps ([a5a4bd1](https://github.com/rearwindowvertigo/ember/commit/a5a4bd1b12a467202ff60fb4ec625cd67399310e))
* emb ground, emb merge and emb cleanup (build step 3) ([6e16176](https://github.com/rearwindowvertigo/ember/commit/6e1617661f6c408b027e97cc1b0de143b72f00b1))
* engines and runs (build step 2) ([4dc042c](https://github.com/rearwindowvertigo/ember/commit/4dc042c874ba025e58b22257ea72e6d9fdb9af8c))
* **engines:** add engine status, model lists and emb engines ([fcec811](https://github.com/rearwindowvertigo/ember/commit/fcec811ffb7bfeb39512dacce96a0e2bb1226442))
* **engines:** add the Claude driver and its host ([e35c920](https://github.com/rearwindowvertigo/ember/commit/e35c920f5bc8e108e05f0a919a99ab35ac248e9d))
* **engines:** add the Codex driver ([db9f50b](https://github.com/rearwindowvertigo/ember/commit/db9f50bdf3440d833d759dec76f1f99d474f01e3))
* **engines:** add the OpenCode driver ([89e31e4](https://github.com/rearwindowvertigo/ember/commit/89e31e4525b0d863b32fd3d813052e3ae9c10449))
* **ground:** add emb ground ([e95965b](https://github.com/rearwindowvertigo/ember/commit/e95965ba6832c914062533827e2522e26e70d146))
* **paths:** add XDG paths with private folder and file modes ([3f6c413](https://github.com/rearwindowvertigo/ember/commit/3f6c413cf26b2a9d78d49995f9ee10230c3c448c))
* **runs:** add the run core, driver interface and emb run ([61e7cef](https://github.com/rearwindowvertigo/ember/commit/61e7cefe0d5cd0bc8b5ec91ed175bbc39d3ab93b))
* **runs:** pass the run's environment to drivers and clean up failed starts ([60a7004](https://github.com/rearwindowvertigo/ember/commit/60a7004c1b9880dafc0bc5f361c53d70c12d51e5))
* **runs:** start runs with no terminal window ([771177e](https://github.com/rearwindowvertigo/ember/commit/771177e2b6e0cc1124a015d5f55aaed8aeb35778))
* **sandbox:** add sandbox methods and checks, and the tmux adapter ([bd16d3f](https://github.com/rearwindowvertigo/ember/commit/bd16d3ffe6319e18b85d4df52d4eb0835c9b970c))
* **sandbox:** one switch per engine, Claude in bubblewrap, driver follow-ups ([7f2c313](https://github.com/rearwindowvertigo/ember/commit/7f2c313bcabf8bf6b222c91550f79dea4733e340))
* **skill:** add emb skill, the branch check and the skill evals ([9397df6](https://github.com/rearwindowvertigo/ember/commit/9397df6141df93aaa01a4332a48b28f488c47d5e))
* **skill:** add the ember skill text and references ([b88da69](https://github.com/rearwindowvertigo/ember/commit/b88da69bc9a8a22cb3ef733c8321779145ab85ea))


### Fixed

* **app:** add labels to their layout before showing them ([b915ad9](https://github.com/rearwindowvertigo/ember/commit/b915ad999cf1d785070e35113a7be57c356b8a01))
* **app:** address shell review findings ([7142800](https://github.com/rearwindowvertigo/ember/commit/7142800f89af09f704947bb067e2c5b353440d8e))
* **app:** check only sandboxed engines and show Sandbox off on Engines ([ae67bd1](https://github.com/rearwindowvertigo/ember/commit/ae67bd1069d8c4aa0b8fd633dc2bbbfc1856d14e))
* **app:** hide the Dependencies state view while tiles show ([0ba9083](https://github.com/rearwindowvertigo/ember/commit/0ba9083c531c0a86e0e19b5fb5ab9442c8120c05))
* **app:** keep Overview rows across reloads ([169ad46](https://github.com/rearwindowvertigo/ember/commit/169ad46647e4510e3ba6200b1d1a895eeb48aec6))
* **app:** keep the Dependencies check spinner until its read ends ([aae526a](https://github.com/rearwindowvertigo/ember/commit/aae526a76d486d8627ee63c301b7f235d145f3af))
* **app:** keep the sidebar focus on the current screen and space icon buttons ([3e70fbc](https://github.com/rearwindowvertigo/ember/commit/3e70fbc34200b62c242467ec0e90700b81ac05e3))
* **app:** let Enter press a focused Run in terminal button ([e2e6413](https://github.com/rearwindowvertigo/ember/commit/e2e6413801ad0f077b80ec4a735e7bb139ad302b))
* **app:** release task callbacks and work on the GUI thread ([0454911](https://github.com/rearwindowvertigo/ember/commit/04549116d5df3de28e2d074f1877588fc164a702))
* **app:** serialise settings saves and skip unchanged choices ([7a0b0eb](https://github.com/rearwindowvertigo/ember/commit/7a0b0ebbd9834d6fa5962e39c2d0cbf6c305527d))
* **branches:** address review findings ([8a61865](https://github.com/rearwindowvertigo/ember/commit/8a61865281be04e431e83ed97a56cfa4f32d2932))
* **choices:** accept OpenRouter model ids whose provider starts with ~ ([606a9df](https://github.com/rearwindowvertigo/ember/commit/606a9dff28c1b7fb143d5ac5bd7991696ff251ce))
* **choices:** wait out first-write contention and narrow fail-closed reads ([5ec962f](https://github.com/rearwindowvertigo/ember/commit/5ec962fb0a261af07fe890eaaee535b79c8af131))
* **codex:** paste the brief instead of putting it on the command line ([a10253c](https://github.com/rearwindowvertigo/ember/commit/a10253cff693bb0b2d5acb39bc173085b2a05872))
* **engines:** fall back to the cache on a broken OpenRouter response ([063450f](https://github.com/rearwindowvertigo/ember/commit/063450fecda0ca2263527f26e3f47b2d608e8258))
* **engines:** list shortlist entries as missing only from a delivered list ([4216957](https://github.com/rearwindowvertigo/ember/commit/42169571de3b1189d3e42364e27e0f5e39cdbf74))
* **engines:** read the Codex model list from standard output only ([6ff0640](https://github.com/rearwindowvertigo/ember/commit/6ff06406dca0f809a21bfde837046f5d0a8090dc))
* **engines:** skip Codex's update prompt in worker sessions ([4835616](https://github.com/rearwindowvertigo/ember/commit/4835616a50b2432adad37d00f541fbbbb190c1ee))
* **engines:** skip OpenCode's compaction messages in foreign input ([dec7580](https://github.com/rearwindowvertigo/ember/commit/dec7580b2393501ca594978f938c0dd63db61c4e))
* **engines:** skip OpenCode's overflow replay in foreign input ([9c19fba](https://github.com/rearwindowvertigo/ember/commit/9c19fba0da7ff99999f3ba9dc246c9dd7916b607))
* **ground:** address review findings ([379c9eb](https://github.com/rearwindowvertigo/ember/commit/379c9eb204b2a5d983098b7d9ab0f7b189fee94f))
* **look:** address review findings ([b340dcc](https://github.com/rearwindowvertigo/ember/commit/b340dccd22c126b0cb2af260ce29ff3dbfb4a6de))
* **paths:** tighten folder creation and the home fallback ([8d150f6](https://github.com/rearwindowvertigo/ember/commit/8d150f6e59d19a109549010c0fa4e9c839402627))
* **release:** match the project in uv.lock by its parsed name value ([83982b6](https://github.com/rearwindowvertigo/ember/commit/83982b6abc830868a3d277857cbb44db0aa1e4f9))
* **runs:** accept owner-typed messages on a forced send ([312c3d5](https://github.com/rearwindowvertigo/ember/commit/312c3d547aa06c01a25618399bd36963295c98eb))
* **runs:** mark incomplete turns and close review gaps in the run core ([538abb7](https://github.com/rearwindowvertigo/ember/commit/538abb7301634fafdc0a31292020e592c19562f4))
* **runs:** point exit 80 at --screen then --send ([7d24063](https://github.com/rearwindowvertigo/ember/commit/7d24063ad8310bba0a95af5fc3f49d0c5300f77d))
* **skill-tools:** leave eval results out of the kit tests ([5b3105c](https://github.com/rearwindowvertigo/ember/commit/5b3105c57e9a4a64a2649b9b95bd41a639daa794))
* **skill,deb:** address the step 5 review findings ([88e9c15](https://github.com/rearwindowvertigo/ember/commit/88e9c153e0059bad32e7a7928ec7e576fd6208d3))
* **skill:** correct two eval stub commits and ask the owner one question ([d7c22b0](https://github.com/rearwindowvertigo/ember/commit/d7c22b01772f038edf9049b45a0910ea901d7ba2))
* **tmux:** run tmux with an empty environment and trim capture rows ([c376341](https://github.com/rearwindowvertigo/ember/commit/c37634177a923835ff1d3e74177ea0005c01bc78))
