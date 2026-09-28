// book_engine.v -- order table + two price-level arrays + control FSM.
//
// Order table: direct-mapped, 2**ORDER_BITS entries indexed by the low
// ORDER_BITS of the ITCH order reference number (NASDAQ assigns references
// sequentially, so the low bits are a near-ideal hash).  The full 64-bit
// reference is stored as a tag.
//   Collision policy: an Add (or the new half of a Replace) whose slot holds a
//   different live order is DROPPED and counted in cnt_coll; the resident order
//   is kept.  Later E/C/X/D/U for a dropped order then miss (cnt_miss).
//
// FSM (one message at a time, no RAM hazards):
//   IDLE : accept record (FIFO head, or bypass straight from decoder), issue read
//   RD   : table data back -> table write + one price-level op, done
//          (Replace: invalidate old + level sub, issue read of new slot)
//   U2   : Replace second half -> insert new order + level add, done
// Initiation interval: 2 cycles (3 for Replace).  Every book message on the
// wire is >= 21 bytes incl. length prefix (> 2 beats), so the engine keeps up
// with the 8 B/cycle bus; the FIFO absorbs local bursts.
module book_engine #(
    parameter ORDER_BITS = 12,
    parameter DEPTH      = 8,
    parameter FIFO_LOG2  = 3
) (
    input  wire                clk,
    input  wire                rst,
    input  wire                in_valid,
    input  wire [2:0]          in_kind,
    input  wire                in_side,
    input  wire [63:0]         in_ref,
    input  wire [63:0]         in_ref2,
    input  wire [31:0]         in_shares,
    input  wire [31:0]         in_price,
    input  wire [31:0]         in_id,
    input  wire [31:0]         in_sof,
    input  wire [31:0]         in_eom,
    output reg                 upd,        // book updated for message upd_id
    output reg  [31:0]         upd_id,
    output reg  [31:0]         upd_sof,
    output reg  [31:0]         upd_eom,
    output wire [DEPTH-1:0]    bid_v,
    output wire [32*DEPTH-1:0] bid_px,
    output wire [32*DEPTH-1:0] bid_q,
    output wire [DEPTH-1:0]    ask_v,
    output wire [32*DEPTH-1:0] ask_px,
    output wire [32*DEPTH-1:0] ask_q,
    output wire                busy,
    output reg  [31:0]         cnt_rel,
    output reg  [31:0]         cnt_coll,
    output reg  [31:0]         cnt_miss,
    output reg  [31:0]         cnt_evict,
    output reg  [31:0]         cnt_drop,
    output reg  [31:0]         cnt_ovf,
    output reg  [7:0]          fifo_max
);
    localparam K_ADD = 3'd1, K_RED = 3'd2, K_DEL = 3'd3, K_REP = 3'd4;
    localparam S_IDLE = 2'd0, S_RD = 2'd1, S_U2 = 2'd2;
    localparam REC_W = 3 + 1 + 64 + 64 + 32 + 32 + 32 + 32 + 32;   // 292
    localparam OB = ORDER_BITS;

    // ---------------- input FIFO with bypass ----------------
    wire [REC_W-1:0] in_rec = {in_kind, in_side, in_ref, in_ref2, in_shares,
                               in_price, in_id, in_sof, in_eom};
    wire             f_empty, f_full, f_ovf;
    wire [REC_W-1:0] f_dout;
    wire [FIFO_LOG2:0] f_cnt;
    reg  [1:0]       st;
    wire             idle   = (st == S_IDLE);
    wire             take   = idle && (!f_empty || in_valid);
    wire [REC_W-1:0] nxt    = f_empty ? in_rec : f_dout;
    wire             f_pop  = idle && !f_empty;
    wire             f_push = in_valid && !(idle && f_empty);

    sync_fifo #(.W(REC_W), .LOG2(FIFO_LOG2)) u_fifo (
        .clk(clk), .rst(rst), .push(f_push), .din(in_rec), .pop(f_pop),
        .dout(f_dout), .empty(f_empty), .full(f_full), .count(f_cnt), .ovf(f_ovf));

    // ---------------- current record ----------------
    reg  [REC_W-1:0] cur;
    wire [2:0]  c_kind = cur[291:289];
    wire        c_side = cur[288];
    wire [63:0] c_ref  = cur[287:224];
    wire [63:0] c_ref2 = cur[223:160];
    wire [31:0] c_sh   = cur[159:128];
    wire [31:0] c_px   = cur[127:96];
    wire [31:0] c_id   = cur[95:64];
    wire [31:0] c_sof  = cur[63:32];
    wire [31:0] c_eom  = cur[31:0];
    wire [63:0] n_ref  = nxt[287:224];
    reg         rep_side;

    // ---------------- order table ----------------
    reg           we;
    reg  [OB-1:0] waddr;
    reg  [129:0]  wdata;
    wire [OB-1:0] raddr = (st == S_RD) ? c_ref2[OB-1:0] : n_ref[OB-1:0];
    wire [129:0]  rdata;
    order_table #(.AW(OB), .W(130)) u_tab (
        .clk(clk), .we(we), .waddr(waddr), .wdata(wdata), .raddr(raddr), .rdata(rdata));

    wire        e_v    = rdata[129];
    wire [63:0] e_ref  = rdata[128:65];
    wire        e_side = rdata[64];
    wire [31:0] e_px   = rdata[63:32];
    wire [31:0] e_sh   = rdata[31:0];
    wire        hit    = e_v && (e_ref == c_ref);
    wire [31:0] dec    = (c_sh < e_sh) ? c_sh : e_sh;
    wire [31:0] rem    = e_sh - dec;
    wire        u2_free = !e_v || (c_ref2[OB-1:0] == c_ref[OB-1:0]);

    // ---------------- price levels ----------------
    reg         lop_v, lop_add, lop_side;
    reg  [31:0] lop_px, lop_q;
    wire        b_ev, b_dr, a_ev, a_dr;
    price_levels #(.DEPTH(DEPTH), .IS_BID(1)) u_bid (
        .clk(clk), .rst(rst), .op_v(lop_v && !lop_side), .op_add(lop_add),
        .op_px(lop_px), .op_q(lop_q), .v_o(bid_v), .px_o(bid_px), .q_o(bid_q),
        .ev(b_ev), .dr(b_dr));
    price_levels #(.DEPTH(DEPTH), .IS_BID(0)) u_ask (
        .clk(clk), .rst(rst), .op_v(lop_v && lop_side), .op_add(lop_add),
        .op_px(lop_px), .op_q(lop_q), .v_o(ask_v), .px_o(ask_px), .q_o(ask_q),
        .ev(a_ev), .dr(a_dr));

    // ---------------- control ----------------
    reg [1:0] n_st;
    reg       done, coll_inc, miss_inc;
    always @* begin
        n_st = st; done = 1'b0; coll_inc = 1'b0; miss_inc = 1'b0;
        we = 1'b0; waddr = c_ref[OB-1:0]; wdata = 130'd0;
        lop_v = 1'b0; lop_add = 1'b0; lop_side = 1'b0; lop_px = 32'd0; lop_q = 32'd0;
        case (st)
            S_IDLE: if (take) n_st = S_RD;
            S_RD: begin
                n_st = S_IDLE;
                done = 1'b1;
                case (c_kind)
                    K_ADD: begin
                        if (!e_v) begin
                            we = 1'b1; wdata = {1'b1, c_ref, c_side, c_px, c_sh};
                            lop_v = 1'b1; lop_add = 1'b1; lop_side = c_side;
                            lop_px = c_px; lop_q = c_sh;
                        end else coll_inc = 1'b1;
                    end
                    K_RED: begin
                        if (hit) begin
                            we = 1'b1; wdata = {(rem != 32'd0), e_ref, e_side, e_px, rem};
                            lop_v = 1'b1; lop_side = e_side; lop_px = e_px; lop_q = dec;
                        end else miss_inc = 1'b1;
                    end
                    K_DEL: begin
                        if (hit) begin
                            we = 1'b1; wdata = {1'b0, e_ref, e_side, e_px, e_sh};
                            lop_v = 1'b1; lop_side = e_side; lop_px = e_px; lop_q = e_sh;
                        end else miss_inc = 1'b1;
                    end
                    default: begin // K_REP
                        if (hit) begin
                            we = 1'b1; wdata = {1'b0, e_ref, e_side, e_px, e_sh};
                            lop_v = 1'b1; lop_side = e_side; lop_px = e_px; lop_q = e_sh;
                            done = 1'b0; n_st = S_U2;
                        end else miss_inc = 1'b1;
                    end
                endcase
            end
            S_U2: begin
                n_st = S_IDLE;
                done = 1'b1;
                waddr = c_ref2[OB-1:0];
                if (u2_free) begin
                    we = 1'b1; wdata = {1'b1, c_ref2, rep_side, c_px, c_sh};
                    lop_v = 1'b1; lop_add = 1'b1; lop_side = rep_side;
                    lop_px = c_px; lop_q = c_sh;
                end else coll_inc = 1'b1;
            end
            default: n_st = S_IDLE;
        endcase
    end

    assign busy = !idle || !f_empty;

    always @(posedge clk) begin
        if (rst) begin
            st <= S_IDLE; cur <= {REC_W{1'b0}}; rep_side <= 1'b0;
            upd <= 1'b0; upd_id <= 32'd0; upd_sof <= 32'd0; upd_eom <= 32'd0;
            cnt_rel <= 0; cnt_coll <= 0; cnt_miss <= 0; cnt_evict <= 0;
            cnt_drop <= 0; cnt_ovf <= 0; fifo_max <= 0;
        end else begin
            st <= n_st;
            if (take) cur <= nxt;
            if (st == S_RD) rep_side <= e_side;
            upd <= done;
            if (done) begin
                upd_id <= c_id; upd_sof <= c_sof; upd_eom <= c_eom;
            end
            cnt_rel   <= cnt_rel + (take ? 32'd1 : 32'd0);
            cnt_coll  <= cnt_coll + (coll_inc ? 32'd1 : 32'd0);
            cnt_miss  <= cnt_miss + (miss_inc ? 32'd1 : 32'd0);
            cnt_evict <= cnt_evict + ((b_ev | a_ev) ? 32'd1 : 32'd0);
            cnt_drop  <= cnt_drop + ((b_dr | a_dr) ? 32'd1 : 32'd0);
            cnt_ovf   <= cnt_ovf + (f_ovf ? 32'd1 : 32'd0);
            if ({{(7-FIFO_LOG2){1'b0}}, f_cnt} > fifo_max) fifo_max <= {{(7-FIFO_LOG2){1'b0}}, f_cnt};
        end
    end
endmodule
