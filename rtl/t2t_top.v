// t2t_top.v -- tick-to-trade pipeline top level.
//
//   in (64b stream) -> itch_parser -> msg_decode -> book_engine -> strategy
//                   -> ouch_tx -> out (64b stream)
//
// Configuration is presented as static ports (would be AXI-Lite registers in a
// real design).  cyc is a free-running cycle counter used to timestamp packets
// for the latency measurement.
module t2t_top #(
    parameter ORDER_BITS = 12,
    parameter DEPTH      = 8,
    parameter FIFO_LOG2  = 3,
    parameter OQ_LOG2    = 3,
    parameter REF_HASH   = 1
) (
    input  wire        clk,
    input  wire        rst,
    // market data in
    input  wire [63:0] in_data,
    input  wire [7:0]  in_keep,
    input  wire        in_valid,
    input  wire        in_last,
    // configuration
    input  wire [15:0] cfg_locate,
    input  wire        cfg_enable,
    input  wire [3:0]  cfg_imb_shift,
    input  wire [31:0] cfg_max_spread,
    input  wire [31:0] cfg_max_qty,
    input  wire [63:0] cfg_stock,
    input  wire [31:0] cfg_firm,
    // order entry out
    output wire        out_valid,
    output wire [63:0] out_data,
    output wire [7:0]  out_keep,
    output wire        out_last,
    // latency + status
    output wire        lat_valid,
    output wire [31:0] lat_pkt,
    output wire [31:0] lat_msg,
    output wire [31:0] lat_id,
    output wire [31:0] lat_min,
    output wire [31:0] lat_max,
    output wire        tob_bid_v,
    output wire [31:0] tob_bid_px,
    output wire [31:0] tob_bid_q,
    output wire        tob_ask_v,
    output wire [31:0] tob_ask_px,
    output wire [31:0] tob_ask_q,
    output wire [31:0] cnt_msgs,
    output wire [31:0] cnt_pkts,
    output wire [31:0] cnt_parse_err,
    output wire [31:0] cnt_rel,
    output wire [31:0] cnt_coll,
    output wire [31:0] cnt_miss,
    output wire [31:0] cnt_evict,
    output wire [31:0] cnt_drop,
    output wire [31:0] cnt_ovf,
    output wire [7:0]  fifo_max,
    output wire [31:0] cnt_orders,
    output wire [31:0] cnt_qovf,
    output wire        busy
);
    localparam CAP = 40;
    reg [31:0] cyc;
    always @(posedge clk) cyc <= rst ? 32'd0 : cyc + 32'd1;

    wire             m_valid;
    wire [8*CAP-1:0] m_data;
    wire [15:0]      m_len;
    wire [31:0]      m_id, m_sof, m_eom;
    itch_parser #(.CAP(CAP)) u_parse (
        .clk(clk), .rst(rst), .in_data(in_data), .in_keep(in_keep),
        .in_valid(in_valid), .in_last(in_last), .now(cyc),
        .msg_valid(m_valid), .msg_data(m_data), .msg_len(m_len), .msg_id(m_id),
        .msg_sof(m_sof), .msg_eom(m_eom), .cnt_msgs(cnt_msgs),
        .cnt_pkts(cnt_pkts), .cnt_err(cnt_parse_err));

    wire        r_valid, r_side;
    wire [2:0]  r_kind;
    wire [63:0] r_ref, r_ref2;
    wire [31:0] r_sh, r_px;
    msg_decode #(.CAP(CAP)) u_dec (
        .msg_valid(m_valid), .msg_data(m_data), .msg_len(m_len),
        .cfg_locate(cfg_locate), .rec_valid(r_valid), .rec_kind(r_kind),
        .rec_side(r_side), .rec_ref(r_ref), .rec_ref2(r_ref2),
        .rec_shares(r_sh), .rec_price(r_px));

    wire                upd;
    wire [31:0]         upd_id, upd_sof, upd_eom;
    wire [DEPTH-1:0]    bid_v, ask_v;
    wire [32*DEPTH-1:0] bid_px, bid_q, ask_px, ask_q;
    wire                eng_busy;
    book_engine #(.ORDER_BITS(ORDER_BITS), .DEPTH(DEPTH), .FIFO_LOG2(FIFO_LOG2),
                  .REF_HASH(REF_HASH)) u_eng (
        .clk(clk), .rst(rst), .in_valid(r_valid), .in_kind(r_kind), .in_side(r_side),
        .in_ref(r_ref), .in_ref2(r_ref2), .in_shares(r_sh), .in_price(r_px),
        .in_id(m_id), .in_sof(m_sof), .in_eom(m_eom),
        .upd(upd), .upd_id(upd_id), .upd_sof(upd_sof), .upd_eom(upd_eom),
        .bid_v(bid_v), .bid_px(bid_px), .bid_q(bid_q),
        .ask_v(ask_v), .ask_px(ask_px), .ask_q(ask_q), .busy(eng_busy),
        .cnt_rel(cnt_rel), .cnt_coll(cnt_coll), .cnt_miss(cnt_miss),
        .cnt_evict(cnt_evict), .cnt_drop(cnt_drop), .cnt_ovf(cnt_ovf),
        .fifo_max(fifo_max));

    assign tob_bid_v  = bid_v[0];
    assign tob_bid_px = bid_px[31:0];
    assign tob_bid_q  = bid_q[31:0];
    assign tob_ask_v  = ask_v[0];
    assign tob_ask_px = ask_px[31:0];
    assign tob_ask_q  = ask_q[31:0];

    wire        f_fire, f_side;
    wire [31:0] f_px, f_q, f_id, f_sof, f_eom;
    strategy u_strat (
        .clk(clk), .rst(rst), .upd(upd), .upd_id(upd_id), .upd_sof(upd_sof),
        .upd_eom(upd_eom), .bid_v(bid_v[0]), .bid_px(bid_px[31:0]),
        .bid_q(bid_q[31:0]), .ask_v(ask_v[0]), .ask_px(ask_px[31:0]),
        .ask_q(ask_q[31:0]), .cfg_enable(cfg_enable), .cfg_imb_shift(cfg_imb_shift),
        .cfg_max_spread(cfg_max_spread), .cfg_max_qty(cfg_max_qty),
        .fire(f_fire), .fire_side(f_side), .fire_px(f_px), .fire_q(f_q),
        .fire_id(f_id), .fire_sof(f_sof), .fire_eom(f_eom));

    ouch_tx #(.QLOG2(OQ_LOG2)) u_tx (
        .clk(clk), .rst(rst), .now(cyc), .fire(f_fire), .fire_side(f_side),
        .fire_px(f_px), .fire_q(f_q), .fire_id(f_id), .fire_sof(f_sof),
        .fire_eom(f_eom), .cfg_stock(cfg_stock), .cfg_firm(cfg_firm),
        .out_valid(out_valid), .out_data(out_data), .out_keep(out_keep),
        .out_last(out_last), .lat_valid(lat_valid), .lat_pkt(lat_pkt),
        .lat_msg(lat_msg), .lat_id(lat_id), .lat_min(lat_min), .lat_max(lat_max),
        .cnt_orders(cnt_orders), .cnt_qovf(cnt_qovf));

    assign busy = eng_busy || upd || out_valid;
endmodule
