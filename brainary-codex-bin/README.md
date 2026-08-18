# brainary-codex 二进制(与官方 codex-bin/ 区分)

brainary-codex fork 的 musl-gcc 交叉构建产物,SWE-bench 容器挂载用。

- 源码: brainary-codex @ 8f16802f3 (poa-demo-coverage)
- 工具链: rust 1.95.0 + x86_64-unknown-linux-musl-gcc (messense/rust-musl-cross:x86_64-musl)
- V8: rusty_v8 150.4.0 ptrcomp_sandbox_release 预编译 (openai/codex releases)
- 构建: cargo build --locked --release --target x86_64-unknown-linux-musl -p codex-cli -p codex-code-mode-host
- 已 strip(未 strip 的原件在 docker volume brainary-target 里,符号化崩溃栈用)

| 文件 | sha256 |
| --- | --- |
| brainary-codex-x86_64-unknown-linux-musl | a1e7530b20bf5b085fadeecae5cfa5ab288d4e663806a58299a7aa55f0a61fcb |
| brainary-codex-code-mode-host-x86_64-unknown-linux-musl | 16f8b24e919d25066c9f9c5288c8716adab470da5c524cd26b3b67fd57482162 |

⚠️ 容器里 host 必须挂成 codex-code-mode-host(同目录固定文件名查找),入口挂成 brainary-codex。
正式跑分入口: ../run_brainary_codex.py

二进制本体不进仓库(单个 244MB,超 GitHub 100MB 上限),按上面的构建命令自己产,
拷进本目录后用上表的 sha256 对一下。
