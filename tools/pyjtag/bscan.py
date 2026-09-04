"""USER1 链 66 位帧协议的主机侧，与板内那一半严格对偶。

帧格式：
    移入  bit[1:0]=CMD(00 NOP / 01 READ / 10 WRITE)  bit[33:2]=ADDR  bit[65:34]=WDATA
    移出  bit[0]=DONE  bit[1]=ERR  bit[33:2]=RDATA

一笔事务 = 一次带命令的 DR 扫描 + 若干次 NOP 轮询扫描（几乎总是 1 次
就 DONE：TCK 一帧 ~11µs，AXI 事务 ~200ns）。
"""

from .xpc import TCK_MODE_WRITE_ONLY

USER1 = 0x02          # 7 系列 IR 操作码：USER1 = 6'b000010
FRAME_BITS = 66

CMD_NOP = 0
CMD_READ = 1
CMD_WRITE = 2
CMD_BURST = 3

BURST_RD = 1 << 50        # burst 头的方向位：1=读
# 写 burst 数据帧的同步字，与板内那一半必须一致。桥在数据帧
# 开头逐位滑动比对到它才开始收数据，因此主机不必知道当前 TCK 档位下 BSCANE2
# 的门控偏移几位。
BURST_SYNC = 0xA5A55A5A


class BscanAxiError(RuntimeError):
    pass


class BscanAxi:
    def __init__(self, tap, max_polls=32):
        self.tap = tap
        self.max_polls = max_polls
        tap.ir_scan(USER1)        # 选中 USER1 链，后续全是 DR 扫描

    def _frame(self, cmd, addr=0, wdata=0):
        val = (cmd & 3) | ((addr & 0xFFFFFFFF) << 2) | ((wdata & 0xFFFFFFFF) << 34)
        return self.tap.dr_scan(val, FRAME_BITS, read=True)

    def _wait_done(self, what):
        for _ in range(self.max_polls):
            out = self._frame(CMD_NOP)
            if out & 1:
                err = (out >> 1) & 1
                rdata = (out >> 2) & 0xFFFFFFFF
                return rdata, err
        raise BscanAxiError(f"{what}: 轮询 {self.max_polls} 次仍未 DONE —— "
                            "bit 里有 JTAG⇆AXI 桥吗？（先 program）")

    def read32(self, addr):
        self._frame(CMD_READ, addr)
        rdata, err = self._wait_done(f"read32(0x{addr:08X})")
        if err:
            raise BscanAxiError(f"read32(0x{addr:08X}): SLVERR")
        return rdata

    def write32(self, addr, data):
        self._frame(CMD_WRITE, addr, data)
        _, err = self._wait_done(f"write32(0x{addr:08X})")
        if err:
            raise BscanAxiError(f"write32(0x{addr:08X}): SLVERR")

    # ── 批量：一次 USB 传输打完上百帧 ──────────────────────────────────
    #
    # 逐笔 read32/write32 要两帧（命令帧 + NOP 轮询帧）、七次 USB 事务，
    # 而 USB 往返 ~180 µs 是 TCK 那 12 µs 的十几倍。批量在两处砍开销：
    #
    #   · 不再发轮询帧。Capture-DR 装载的本就是「进入本帧时」的状态，也就是
    #     上一笔的结果，所以 M 笔命令背靠背发，第 k 帧移出的即第 k-1 笔的结果，
    #     末尾补一帧 NOP 收最后一笔。M 笔从 2M 帧降到 M+1 帧。
    #     帧间隔 71 个 TCK（6 MHz 下 11.8 µs）远长于 AXI 事务的 ~100 ns，
    #     所以 DONE 必然已经立起；仍然逐帧核对 DONE，不满足就回退逐笔轮询。
    #   · 整批帧拼成一条位流交给 Tap.dr_scan_batch，一次传输 115 帧。
    def _batch(self, cmds, what):
        """cmds = [(cmd, addr, wdata), ...] → [(rdata, err), ...]，一一对应。

        每笔命令后跟一帧 NOP。板上实测：命令帧 Update 之后，结果要到**下下帧**的
        Capture 才可靠 —— DONE 是 `req_tgl == ack_tgl` 经两级同步器进 DRCK 域的，
        而 DRCK 是门控的，只在 Capture/Shift 期间有脉冲，一帧给不够同步沿。
        （现象：命令帧的下一帧 DONE=0 而 RDATA 已经更新，数据路径比握手位快。）
        所以帧序列是 cmd·NOP·cmd·NOP…·NOP，cmd_k 的结果落在 outs[2k+2]。
        """
        vals = []
        for c, a, w in cmds:
            vals.append((c & 3) | ((a & 0xFFFFFFFF) << 2) | ((w & 0xFFFFFFFF) << 34))
            vals.append(CMD_NOP)
        vals.append(CMD_NOP)                      # 收尾，取最后一笔的结果
        outs = self.tap.dr_scan_batch(vals, FRAME_BITS)
        res = []
        for k in range(len(cmds)):
            out = outs[2 * k + 2]
            if not out & 1:
                raise BscanAxiError(f"{what}: 第 {k} 笔 DONE=0")
            res.append(((out >> 2) & 0xFFFFFFFF, (out >> 1) & 1))
        return res

    def write_burst(self, addr, words):
        """连续写：一次 burst 把 words 灌到 addr 起的连续地址。

        两帧：66 位头（CMD=11 · 起始地址 · 字数 · 方向=写）+ 数据帧。数据帧是
        同步字 + len(words)×32 位，不采 TDO，主机侧因此走 FX2 的纯写快路径，
        协议效率 32/32（单笔是 32/71）。

        同步字让桥自己对齐位边界，见 bscan_axi_bridge.py 的「写方向的同步字」：
        BSCANE2 的门控信号在高 TCK 档下相对 TCK 偏移一拍，没有它 12 MHz 档整条
        数据流会错位一位。

        整段不采 TDO，所以自动切到 12 MHz 档跑完再切回（两次切档约 500 µs，
        1024 字一笔就能赚回来）。要连着发很多笔时，调用方先自己
        `cable.set_tck(TCK_MODE_WRITE_ONLY)`，这里就不会反复切。

        桥的 err_q 不累积，burst 中途的 SLVERR 只能由随后一帧的 ERR 带出来
        （连同 bov 溢出标志），所以要精确定位错误请用 write32_many(verify=True)。
        """
        n = len(words)
        if not n:
            return
        head = (CMD_BURST & 3) | ((addr & 0xFFFFFFFF) << 2) | ((n & 0xFFFF) << 34)
        payload = BURST_SYNC.to_bytes(4, "little") + b"".join(
            int(w & 0xFFFFFFFF).to_bytes(4, "little") for w in words)
        cable = self.tap.cable
        prev = cable.tck_mode
        if prev != TCK_MODE_WRITE_ONLY:
            cable.set_tck(TCK_MODE_WRITE_ONLY)      # 数据帧不采 TDO，可以走 12 MHz
        try:
            cable.shift_dr_head_stream(head, FRAME_BITS, payload)
        finally:
            if prev != TCK_MODE_WRITE_ONLY:
                cable.set_tck(prev)

    def read_burst(self, addr, n):
        """连续读：一次 burst 从 addr 起取回 n 个字。

        两帧：66 位头（方向位=读）+ n×32 位纯移出。桥的 aclk 域自主预取供数，
        协议效率同样是 32/32 —— 而逐笔 read32_many 是「命令帧 + NOP 帧」各 71 位
        才换 32 位数据，只有 23%。采 TDO 的位速率（1.19 M 位/秒）两者相同，
        所以这里的提速全部来自协议效率。

        数据帧要采 TDO，必须在能正确采样的档位下调用（见 xpc.TCK_MODE_FAST）。
        """
        if n <= 0:
            return []
        head = ((CMD_BURST & 3) | ((addr & 0xFFFFFFFF) << 2)
                | ((n & 0xFFFF) << 34) | BURST_RD)
        self.tap.dr_scan_batch([head], FRAME_BITS, nsample=0)
        raw = self.tap.dr_scan_long(b"\x00" * (n * 4), n * 32, nsample=n * 32)
        return [int.from_bytes(raw[4 * i:4 * i + 4], "little") for i in range(n)]

    def read32_many(self, addrs):
        """批量读，返回与 addrs 等长的数据列表。"""
        res = self._batch([(CMD_READ, a, 0) for a in addrs], "read32_many")
        for (_, err), a in zip(res, addrs):
            if err:
                raise BscanAxiError(f"read32(0x{a:08X}): SLVERR")
        return [d for d, _ in res]

    def write32_many(self, pairs, verify=True):
        """批量写，pairs = [(addr, data), ...]。

        verify=True  每帧采 TDO，逐笔核对 DONE/ERR（0.06 MB/s）。
        verify=False 整批不采 TDO，末尾补一帧单独查一次（0.26 MB/s，快 4.3 倍）。

        为什么快这么多：固件只要传输里有一位采样，整段就走慢路径
        （5.21 → 1.16 MHz），且与采样多少无关 —— 实测每帧采 16 / 48 / 66 位
        耗时一样，所以要省就得全省。

        verify=False 走一帧一笔、背靠背不插 NOP：DONE 位虽然要两帧才反映过来，
        但事务本身照常执行 —— 板上实测背靠背连写 512 笔再逐笔读回，全部一致。

        verify=False 的代价：桥的 err_q 每笔事务直接覆盖、不累积
        （见 bscan_axi_bridge.py 的 axi_fsm），所以中间某笔 SLVERR 会被
        后一笔冲掉，末尾只查得到最后一笔。装载大块数据这类「地址一次算对、
        错就通篇错」的场景用它；地址逐笔不同又要精确定位错误时用 verify=True。
        """
        if verify:
            res = self._batch([(CMD_WRITE, a, d) for a, d in pairs], "write32_many")
            for (_, err), (a, _d) in zip(res, pairs):
                if err:
                    raise BscanAxiError(f"write32(0x{a:08X}): SLVERR")
            return
        vals = [(CMD_WRITE & 3) | ((a & 0xFFFFFFFF) << 2) | ((d & 0xFFFFFFFF) << 34)
                for a, d in pairs]
        self.tap.dr_scan_batch(vals, FRAME_BITS, nsample=0)
        rdata, err = self._wait_done("write32_many(verify=False) 末帧")
        if err:
            raise BscanAxiError("write32_many: 末笔 SLVERR（中间若干笔的错已被覆盖，"
                                "要精确定位改用 verify=True）")
