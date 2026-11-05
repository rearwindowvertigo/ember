# Changelog

## [0.15.0](https://github.com/rearwindowvertigo/ember/compare/v0.14.1...v0.15.0) (2026-11-05)


### Added

* **app:** collapse and hide the sidebar in narrow windows ([7dbf345](https://github.com/rearwindowvertigo/ember/commit/7dbf34521ee3ffdb6800854d587bc135fe5f5171))
* **app:** fit every screen to the window width ([205dc94](https://github.com/rearwindowvertigo/ember/commit/205dc946385b8cee615a70c5914a24181f3d9d7d))
* **cli:** limit emb cleanup to one claim with --claim ([ef81228](https://github.com/rearwindowvertigo/ember/commit/ef812285aa1eef6a3418772ddd6b163cbf488132))
* **flows:** ask gap checks and reviewers for the cause and other places ([417b5e9](https://github.com/rearwindowvertigo/ember/commit/417b5e9efd27aff4d68ffb8d6588606a1691dac9))
* **flows:** list merged sibling tasks in a task's reviewer brief ([145a921](https://github.com/rearwindowvertigo/ember/commit/145a921bf921d62438128a71fa85114f7f78bade))
* **runs:** print the next command at every step of an emb run task ([bb9c576](https://github.com/rearwindowvertigo/ember/commit/bb9c576598a01f1403e28e4f8d43bf0c51790aee))
* **runs:** qualify the sandbox owner line when the turn exited 0 ([f8385b3](https://github.com/rearwindowvertigo/ember/commit/f8385b3ae78c6e1fea3eea39703db10f43795e42))
* **skill:** repeat eval runs and count passes per grader ([d85fe1c](https://github.com/rearwindowvertigo/ember/commit/d85fe1cf919d39a11d032d93310e6a2abfedcc75)), closes [#149](https://github.com/rearwindowvertigo/ember/issues/149)


### Fixed

* **app:** settle the OpenCode table height test under load ([f71e2c6](https://github.com/rearwindowvertigo/ember/commit/f71e2c6dcb7afd5fcd99c6b4262641c57131e1cc))
* **cli:** refuse an empty read file in emb gapcheck and emb flow ([369d15c](https://github.com/rearwindowvertigo/ember/commit/369d15ccf07345b9e70271b999dfcc20c5d8c113))
* **engines:** give OpenCode 2 time to take a run's first message ([b40ba2b](https://github.com/rearwindowvertigo/ember/commit/b40ba2bae1c9882d6d50d7d4224f9f99d6809cc3))
* **flows:** mark which review produced each finding in emb flow --status ([26bc49d](https://github.com/rearwindowvertigo/ember/commit/26bc49d7d12a66a200262716720e712efb1287d3))
* **flows:** recognise .test., .spec. and __tests__ test files ([ae63673](https://github.com/rearwindowvertigo/ember/commit/ae636735c4919f02ec375b2cb6d88c4eeb5516ce))
* **runs:** check Codex project trust in emb run --dry-run ([c5f8b8e](https://github.com/rearwindowvertigo/ember/commit/c5f8b8eb76bb5f0eac1cdabdc786de1d3bdebaab))
* **runs:** do not report flow runs orphaned while the flow waits ([fcd2d48](https://github.com/rearwindowvertigo/ember/commit/fcd2d48d1e989a75994979cc319e527425682bbb))

## [0.14.1](https://github.com/rearwindowvertigo/ember/compare/v0.14.0...v0.14.1) (2026-11-04)


### Fixed

* **app:** keep the OpenCode model table inside the Engines screen ([f536c2c](https://github.com/rearwindowvertigo/ember/commit/f536c2c7c730054d6ee2989ce64deb3ff22697cc)), closes [#120](https://github.com/rearwindowvertigo/ember/issues/120)

## [0.14.0](https://github.com/rearwindowvertigo/ember/compare/v0.13.0...v0.14.0) (2026-11-04)


### ⚠ BREAKING CHANGES

* **opencode:** OpenCode model ids are full OpenCode ids (`<integration>/<model>`, such as `openrouter/deepseek/deepseek-v4.1-flash-1017` or `anthropic/claude-haiku-4-5`) in `emb engines --models opencode`, `emb run --model` and saved settings. Settings, run, flow and panel records written by 0.13.0 are read with their OpenCode models as `openrouter/<model>`.

### Added

* **app:** show and filter OpenCode models by provider ([b4f081d](https://github.com/rearwindowvertigo/ember/commit/b4f081d092a21a86a2f8f8cd0851c9af19eb8a73))
* **opencode:** list and run every connected integration's models ([b4f081d](https://github.com/rearwindowvertigo/ember/commit/b4f081d092a21a86a2f8f8cd0851c9af19eb8a73)), closes [#112](https://github.com/rearwindowvertigo/ember/issues/112)
* warn about cited sources a worker cannot read ([641ddcf](https://github.com/rearwindowvertigo/ember/commit/641ddcf84d17384b486befa1f1c61a840697aa06)), closes [#110](https://github.com/rearwindowvertigo/ember/issues/110)


### Fixed

* match whole cited references and use every read path ([9cb3cec](https://github.com/rearwindowvertigo/ember/commit/9cb3cecc0cbf345b6c1938274d5ccf4ad3533a00)), closes [#110](https://github.com/rearwindowvertigo/ember/issues/110)
* **sandbox:** stop offering read access for shell startup files ([b4f081d](https://github.com/rearwindowvertigo/ember/commit/b4f081d092a21a86a2f8f8cd0851c9af19eb8a73)), closes [#114](https://github.com/rearwindowvertigo/ember/issues/114)
* **skill:** stop telling the lead how a worker is billed ([b4f081d](https://github.com/rearwindowvertigo/ember/commit/b4f081d092a21a86a2f8f8cd0851c9af19eb8a73))

## [0.13.0](https://github.com/rearwindowvertigo/ember/compare/v0.12.0...v0.13.0) (2026-11-04)


### ⚠ BREAKING CHANGES

* emb merge requires --subject for an Ember run branch and lands it as one squash commit with Ember-Verdict and Ember-Task-Tip trailers; flow tasks land as one squash commit each.
* **panel:** the list form of emb panel --status --json gives member_count and running_members in place of members.
* **gapcheck:** gap check results give each gap an id, store answers once in steps, and list ranked gaps by reference; emb gapcheck --previous and --answer read and write this format.

### Added

* add emb run --dry-run and a start line naming the resolved worker ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))
* classify provider errors and retry capacity errors with Team-set limits ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))
* detect a stale loaded ember skill at each claim ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))
* **gapcheck:** carry settled gap answers into repeat gap checks ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0)), closes [#102](https://github.com/rearwindowvertigo/ember/issues/102)
* name the hidden folders a sandboxed worker was denied ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))
* **panel:** show panel members' progress while a panel runs ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0)), closes [#98](https://github.com/rearwindowvertigo/ember/issues/98)
* run read-only runs and panel members in a detached worktree ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))
* show each turn's result in emb run --status ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))
* squash worker branches at emb merge under a lead-written subject ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0)), closes [#94](https://github.com/rearwindowvertigo/ember/issues/94)


### Fixed

* **app:** drop the Overview subtitle and collapse the empty Live card ([5bef095](https://github.com/rearwindowvertigo/ember/commit/5bef0950a6c85dda521206bde6d218e030324ff7))
* **app:** keep the Dependencies scroll bar clear of the tiles ([da37777](https://github.com/rearwindowvertigo/ember/commit/da377776c79da8499c799f3e96c5680f8e5ba8ee))
* create worktrees outside the repository checkout ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0)), closes [#93](https://github.com/rearwindowvertigo/ember/issues/93)
* **flow:** record a hand-merged conflict task as merged ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))
* keep and report runs whose starting command ended ([4f19cff](https://github.com/rearwindowvertigo/ember/commit/4f19cff5a95352b146a71e4b38d010de906557c0))

## [0.12.0](https://github.com/rearwindowvertigo/ember/compare/v0.11.0...v0.12.0) (2026-11-04)


### Added

* **deps:** read OpenCode's newest release from GitHub and offer the major upgrade ([2de4abc](https://github.com/rearwindowvertigo/ember/commit/2de4abc86c066cfcefe17ed9fe8cf65abc256b0b))
* **engines:** run OpenCode 2 workers through a private server, chosen by the installed version ([2de4abc](https://github.com/rearwindowvertigo/ember/commit/2de4abc86c066cfcefe17ed9fe8cf65abc256b0b))


### Fixed

* **app:** use the tray's activation token on Quit with the window hidden ([2de4abc](https://github.com/rearwindowvertigo/ember/commit/2de4abc86c066cfcefe17ed9fe8cf65abc256b0b))
* **engines:** check OpenCode 2 sign-in without starting its background service ([2de4abc](https://github.com/rearwindowvertigo/ember/commit/2de4abc86c066cfcefe17ed9fe8cf65abc256b0b))
* **runs:** keep the engine's own folder in the sandbox when a script starts it ([2de4abc](https://github.com/rearwindowvertigo/ember/commit/2de4abc86c066cfcefe17ed9fe8cf65abc256b0b))

## [0.11.0](https://github.com/rearwindowvertigo/ember/compare/v0.10.0...v0.11.0) (2026-11-04)


### Added

* **app:** fit the Engines cards to the window width ([a5d70ae](https://github.com/rearwindowvertigo/ember/commit/a5d70ae2a8209ea4fb9bfe964e3343d3cd08cde5)), closes [#74](https://github.com/rearwindowvertigo/ember/issues/74)
* **app:** let only an info icon open a tooltip ([0ab7fda](https://github.com/rearwindowvertigo/ember/commit/0ab7fda3533093cfea3a56da72210759365ead58)), closes [#81](https://github.com/rearwindowvertigo/ember/issues/81)
* **app:** one row convention, rounded tooltips, and clearer Team pickers ([e7f0e9a](https://github.com/rearwindowvertigo/ember/commit/e7f0e9a9b63d00c79969fd917eb29779a300f5e9)), closes [#68](https://github.com/rearwindowvertigo/ember/issues/68) [#72](https://github.com/rearwindowvertigo/ember/issues/72)
* **app:** show Elapsed, run subjects and panel questions on Overview ([0bde2ae](https://github.com/rearwindowvertigo/ember/commit/0bde2ae4362b27d98c204867bf6e9edd244c1bb1)), closes [#69](https://github.com/rearwindowvertigo/ember/issues/69) [#73](https://github.com/rearwindowvertigo/ember/issues/73) [#77](https://github.com/rearwindowvertigo/ember/issues/77)


### Fixed

* **app:** bring fields, lists, paths and unavailable actions in line with the design system ([63b5ef6](https://github.com/rearwindowvertigo/ember/commit/63b5ef62d0f638879fd58fe6903c55e3b117cde9)), closes [#71](https://github.com/rearwindowvertigo/ember/issues/71)
* **app:** close tooltips on Escape anywhere and keep them on screen ([3e2a39a](https://github.com/rearwindowvertigo/ember/commit/3e2a39a23c3ab50ce55a9de409589850470399b6)), closes [#68](https://github.com/rearwindowvertigo/ember/issues/68)
* **app:** count the Engines content margins when fitting card columns ([3da899e](https://github.com/rearwindowvertigo/ember/commit/3da899ebfcec1f3cddff3b8612c559cd4a691896)), closes [#74](https://github.com/rearwindowvertigo/ember/issues/74)
* **app:** pass the launch's activation token to the running Ember ([4a81f87](https://github.com/rearwindowvertigo/ember/commit/4a81f870a572b2cbf740304e6328c2ab31e9e0d4)), closes [#80](https://github.com/rearwindowvertigo/ember/issues/80)
* **app:** satisfy the type check for the closed picker text and row tests ([3e7cf48](https://github.com/rearwindowvertigo/ember/commit/3e7cf487604cc54cbc773bcbd8936a10cf851313))
* **app:** say why Add member is hidden when every shortlisted model is a member ([97e119b](https://github.com/rearwindowvertigo/ember/commit/97e119baa76fe9c10eea043cec57b2da2ec3a043)), closes [#71](https://github.com/rearwindowvertigo/ember/issues/71)
* **app:** show a live run of an ended flow or panel in Live ([b3eb14c](https://github.com/rearwindowvertigo/ember/commit/b3eb14c86c554b0e1f6a14d50ad97987bbf2ed16)), closes [#70](https://github.com/rearwindowvertigo/ember/issues/70)
* **deps:** follow Claude Code's update channel and never show newest below installed ([b23b8cd](https://github.com/rearwindowvertigo/ember/commit/b23b8cded320fda2f7a53fafe609fa21e7154264)), closes [#79](https://github.com/rearwindowvertigo/ember/issues/79)
* **deps:** read the newest version again after a release channel change ([39ae52e](https://github.com/rearwindowvertigo/ember/commit/39ae52e2295cbcebf08e270663cfcd5e6ae6864c)), closes [#79](https://github.com/rearwindowvertigo/ember/issues/79)
* **engines:** leave what a read folder shows to the sandbox ([ec560af](https://github.com/rearwindowvertigo/ember/commit/ec560afc8a523a28fb7f52f02ce74e265c067f02)), closes [#75](https://github.com/rearwindowvertigo/ember/issues/75)
* **engines:** let OpenCode read its read paths and show its session ([fadb6de](https://github.com/rearwindowvertigo/ember/commit/fadb6de56fa416c3929cb11730a650e42bf7a959)), closes [#75](https://github.com/rearwindowvertigo/ember/issues/75) [#76](https://github.com/rearwindowvertigo/ember/issues/76)
* **engines:** open a read file's folder only where the sandbox hides it ([2b23ebf](https://github.com/rearwindowvertigo/ember/commit/2b23ebf75c82914541eb8784be0aff839aefc983)), closes [#75](https://github.com/rearwindowvertigo/ember/issues/75)

## [0.10.0](https://github.com/rearwindowvertigo/ember/compare/v0.9.0...v0.10.0) (2026-11-03)


### Added

* **app:** name the Team limit settings as maximums ([e49e7d7](https://github.com/rearwindowvertigo/ember/commit/e49e7d76b538ffa10142a1a5497dc77a00b98482))
* **app:** wrap tooltips and show setting tooltips on an info icon ([0ee0299](https://github.com/rearwindowvertigo/ember/commit/0ee0299dd8a2f09bfbb0bde107bf98568920ddf6)), closes [#55](https://github.com/rearwindowvertigo/ember/issues/55)


### Fixed

* **app:** keep the info tooltip open after Tab and wrap unbroken text ([df98113](https://github.com/rearwindowvertigo/ember/commit/df98113a498b5cdd26818221c8cdbf950b984241))

## [0.9.0](https://github.com/rearwindowvertigo/ember/compare/v0.8.0...v0.9.0) (2026-11-03)


### Added

* **app:** Planning, Flows and Claude Workflows settings on Team ([cd6a171](https://github.com/rearwindowvertigo/ember/commit/cd6a171b90d40e864add403238e7e734adf1ebac)), closes [#62](https://github.com/rearwindowvertigo/ember/issues/62)
* **app:** rename the Menu screen to Team ([2c15024](https://github.com/rearwindowvertigo/ember/commit/2c15024a6d0ecbb162561e0f61b612baf61aba9d)), closes [#61](https://github.com/rearwindowvertigo/ember/issues/61)
* **app:** show an installed update in the sidebar, not as a top alert ([43c1ff8](https://github.com/rearwindowvertigo/ember/commit/43c1ff82b24edda3f21019828f4e191d5d883ecc)), closes [#59](https://github.com/rearwindowvertigo/ember/issues/59)
* **flow:** gap check, flow and Workflow settings in emb choices ([b80548e](https://github.com/rearwindowvertigo/ember/commit/b80548e9c4e3064d89166c5493fbe4f150d1530d)), closes [#62](https://github.com/rearwindowvertigo/ember/issues/62)


### Fixed

* **app:** fit Dependencies columns to the scroll area's own size ([6a679ee](https://github.com/rearwindowvertigo/ember/commit/6a679eeb037cb3bb80104c9edefa4ac5c2d0e286)), closes [#60](https://github.com/rearwindowvertigo/ember/issues/60)
* **app:** keep Dependencies tiles clear of the scroll bar ([0d40bbd](https://github.com/rearwindowvertigo/ember/commit/0d40bbdbc6e71a2dc1fae19bb402765a09df1df9)), closes [#60](https://github.com/rearwindowvertigo/ember/issues/60)
* **flow:** ask about every gap when settings have a source error ([a326618](https://github.com/rearwindowvertigo/ember/commit/a32661855884cca0a4e34c6d6cc7160ca7aaa42a)), closes [#62](https://github.com/rearwindowvertigo/ember/issues/62)
* **skill:** decide the gap check before any reviewer step ([ae05189](https://github.com/rearwindowvertigo/ember/commit/ae05189d09c4a8e1228e450e795d2a489c6e7c1e)), closes [#62](https://github.com/rearwindowvertigo/ember/issues/62)

## [0.8.0](https://github.com/rearwindowvertigo/ember/compare/v0.7.0...v0.8.0) (2026-11-03)


### Added

* **app:** replace the Clear caption with a tooltip ([76e84c9](https://github.com/rearwindowvertigo/ember/commit/76e84c9d222232b4b57cdeb626313012b6d85acd)), closes [#56](https://github.com/rearwindowvertigo/ember/issues/56)
* **app:** show each run's result and times on its detail line ([76e84c9](https://github.com/rearwindowvertigo/ember/commit/76e84c9d222232b4b57cdeb626313012b6d85acd)), closes [#54](https://github.com/rearwindowvertigo/ember/issues/54)
* **app:** show the restart offer on its own after an upgrade ([ad30a4c](https://github.com/rearwindowvertigo/ember/commit/ad30a4c59b1c25bbb94eee6c183a71a7cebee34c)), closes [#38](https://github.com/rearwindowvertigo/ember/issues/38)

## [0.7.0](https://github.com/rearwindowvertigo/ember/compare/v0.6.0...v0.7.0) (2026-11-03)


### Added

* **app:** put the row type in the title and centre Overview rows ([59be2ad](https://github.com/rearwindowvertigo/ember/commit/59be2adb84155e941c1e964daac1a36c3449eef9)), closes [#51](https://github.com/rearwindowvertigo/ember/issues/51)

## [0.6.0](https://github.com/rearwindowvertigo/ember/compare/v0.5.0...v0.6.0) (2026-11-03)


### Added

* **app:** remove the screen mirror from Overview ([9534226](https://github.com/rearwindowvertigo/ember/commit/9534226f78a070b389a46912fc78a1ba34c94f95)), closes [#47](https://github.com/rearwindowvertigo/ember/issues/47)


### Fixed

* **app:** show the window when a tray terminal click fails ([1f62e25](https://github.com/rearwindowvertigo/ember/commit/1f62e25bd9ef61358f438b35eddd0f95907545a9)), closes [#37](https://github.com/rearwindowvertigo/ember/issues/37)
* **choices:** say what a finished claim was before the finish ([c2e8111](https://github.com/rearwindowvertigo/ember/commit/c2e8111ca54d213b981b03222178db40e16b7dd9)), closes [#45](https://github.com/rearwindowvertigo/ember/issues/45)
* **flow:** accept --drop for a pending task ([ee32580](https://github.com/rearwindowvertigo/ember/commit/ee32580b3cef833a4cdf47feac0389171e2388e7)), closes [#44](https://github.com/rearwindowvertigo/ember/issues/44)
* **run:** end a worker's leftover processes when its run closes ([48cde67](https://github.com/rearwindowvertigo/ember/commit/48cde67696d82579c8d464a19bbd3d68cef02ce6)), closes [#46](https://github.com/rearwindowvertigo/ember/issues/46)

## [0.5.0](https://github.com/rearwindowvertigo/ember/compare/v0.4.0...v0.5.0) (2026-11-03)


### ⚠ BREAKING CHANGES

* **deps:** emb deps --json no longer has the ember_release field, and emb deps text output no longer ends with an Ember release line.

### Added

* **app:** offer a restart when an upgrade finds Ember running ([7e89275](https://github.com/rearwindowvertigo/ember/commit/7e89275d004176f1333aec58f474e7c98bec0557))
* **app:** show start times on nested runs and fold them in Finished ([530851c](https://github.com/rearwindowvertigo/ember/commit/530851ca98258fce5916d413f7964d30f6f0b917))


### Fixed

* **app:** give Engines' Check now the refresh icon ([c53d8cf](https://github.com/rearwindowvertigo/ember/commit/c53d8cfe50262bad301babb4c6f6110186f8a06c))
* **deps:** remove the Ember update notice ([f4a4acf](https://github.com/rearwindowvertigo/ember/commit/f4a4acf2cba3c7ea07b618c0de3acf4df5aed1c4))

## [0.4.0](https://github.com/rearwindowvertigo/ember/compare/v0.3.0...v0.4.0) (2026-11-03)


### Added

* **app:** lead Overview with Live and preview Finished ([fa2cead](https://github.com/rearwindowvertigo/ember/commit/fa2cead9abce9e514eaf4edeec83d4c2d969b5e8))
* **flows:** set flow review rounds on the Menu screen ([400c59b](https://github.com/rearwindowvertigo/ember/commit/400c59bdd521e60591f1a5b609ee4949d6c67888))


### Fixed

* **app:** give the app icon a charcoal tile that fills its slot ([6708236](https://github.com/rearwindowvertigo/ember/commit/6708236b65d0f311c15fb5004eebe1fd456e40bc))
* **app:** show a disabled Clear as disabled ([f1ff20c](https://github.com/rearwindowvertigo/ember/commit/f1ff20c78e5d568c92e25f05c80993fe4cbc5862))
* **app:** show the focus ring only for keyboard focus ([034279c](https://github.com/rearwindowvertigo/ember/commit/034279c975ecc6dc16f0b55398a6810496419fd5))
* **app:** solid borders, a picker arrow and short rounded chips ([1e5924c](https://github.com/rearwindowvertigo/ember/commit/1e5924c29373fa8dc851c166ba2acfb75e07812c))

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
