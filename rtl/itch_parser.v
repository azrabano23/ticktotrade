// itch_parser.v -- MoldUDP64 de-framer + ITCH message extractor.
//
// Input: 64-bit streaming bus, 8 bytes/beat, AXI-Stream byte order (byte 0 of
// the stream is in_data[7:0]).  One MoldUDP64 packet (the UDP payload) per
// in_last-terminated frame.  in_keep must be contiguous from lane 0; only the
// last beat of a frame may be partial.
//
// MoldUDP64 packet: Session(10) Seq(8) Count(2) then repeated
//   { MsgLen(2, big endian), Msg(MsgLen) }.
//
// Straddling: a message and its 2-byte length prefix may start at any byte
// lane and span any number of beats (the prefix itself may be split across
// two beats).  The design is *not* a serial byte loop: each beat is handled
// in parallel using one invariant --
//
//   every message is >= 7 bytes (ITCH 5.0 minimum is 12), so a single 8-byte
//   beat contains at most ONE length prefix and at most ONE message end.
//
// Per beat:  region lanes [0, min(skip,n))  continue the current region
//            (header, message body, or nothing);  if the region ends inside
//            the beat, the length prefix sits at lane p = skip, and the new
//            message body starts at lane p+2.  A split prefix (high byte in
//            lane 7) is carried to lane 0 of the next beat.
// Message bytes are captured into a CAP-byte buffer with one 8:1 byte mux per
// buffer position (body continuation) plus a small mux for a new body.
//
// Error policy (cnt_err, once per packet): MsgLen < 7, or packet ends inside a
// header / length prefix / message.  After MsgLen < 7 the rest of the packet
// is discarded (we cannot resynchronise inside a packet).
module itch_parser #(
    parameter CAP = 40
) (
    input  wire              clk,
    input  wire              rst,
    input  wire [63:0]       in_data,
    input  wire [7:0]        in_keep,
    input  wire              in_valid,
    input  wire              in_last,
    input  wire [31:0]       now,        // free-running cycle counter
    output reg               msg_valid,
    output reg [8*CAP-1:0]   msg_data,   // byte j of the message at [8j+7:8j]
    output reg [15:0]        msg_len,
    output reg [31:0]        msg_id,     // global message sequence (from 0)
    output reg [31:0]        msg_sof,    // cycle of first beat of the packet
    output reg [31:0]        msg_eom,    // cycle of beat holding the last byte
    output reg [31:0]        cnt_msgs,
    output reg [31:0]        cnt_pkts,
    output reg [31:0]        cnt_err
);
    localparam HDR = 16'd20;
    localparam MINLEN = 16'd7;

    // ---------------- state ----------------
    reg [15:0]      skip;       // bytes left in current region at beat start
    reg             body;       // current region is a message body
    reg             lp;         // high length byte seen, low byte is lane 0
    reg [7:0]       len_hi;
    reg [15:0]      mpos;       // position of next body byte
    reg [15:0]      mlen;
    reg             disc;       // discarding rest of packet
    reg [8*CAP-1:0] cur;
    reg             sop;
    reg [31:0]      sof_q;

    // ---------------- lanes ----------------
    reg [3:0] n;                // number of valid lanes (keep contiguous)
    always @* begin
        casez (in_keep)
            8'b1???????: n = 4'd8;
            8'b01??????: n = 4'd7;
            8'b001?????: n = 4'd6;
            8'b0001????: n = 4'd5;
            8'b00001???: n = 4'd4;
            8'b000001??: n = 4'd3;
            8'b0000001?: n = 4'd2;
            8'b00000001: n = 4'd1;
            default:     n = 4'd0;
        endcase
    end

    function [7:0] lane;
        input [63:0] d;
        input [2:0]  k;
        lane = d[8*k +: 8];
    endfunction

    // region lanes this beat
    wire        region_ends = (skip <= {12'd0, n});
    wire [3:0]  pre   = region_ends ? skip[3:0] : n;
    wire [3:0]  p     = skip[3:0];                      // prefix lane if region_ends

    // ---------------- writer 1: body continuation ----------------
    reg  [8*CAP-1:0] buf1;
    reg  [15:0]      dj;
    integer          j;
    always @* begin
        buf1 = cur;
        for (j = 0; j < CAP; j = j + 1) begin
            dj = j[15:0] - mpos;
            if (body && !lp && (mpos <= j) && (dj < {12'd0, pre}))
                buf1[8*j +: 8] = lane(in_data, dj[2:0]);
        end
    end

    // ---------------- prefix / new body ----------------
    reg         newbody;
    reg [15:0]  L;
    reg [3:0]   s;       // first lane of new body
    always @* begin
        newbody = 1'b0; L = 16'd0; s = 4'd0;
        if (lp) begin
            newbody = 1'b1; L = {len_hi, lane(in_data, 3'd0)}; s = 4'd1;
        end else if (region_ends && (p + 4'd2 <= n)) begin
            newbody = 1'b1; L = {lane(in_data, p[2:0]), lane(in_data, p[2:0] + 3'd1)};
            s = p + 4'd2;
        end
    end
    wire [3:0] avail = n - s;      // new-body bytes in this beat

    // ---------------- writer 2: new body head ----------------
    reg [8*CAP-1:0] buf2;
    reg [3:0]       sj;
    integer         i;
    always @* begin
        buf2 = buf1;
        for (i = 0; i < 7; i = i + 1) begin
            sj = s + i[3:0];
            if (i[3:0] < avail) buf2[8*i +: 8] = lane(in_data, sj[2:0]);
        end
    end

    // ---------------- next state ----------------
    reg [15:0] n_skip, n_mpos, n_mlen, d_len;
    reg        n_body, n_lp, n_disc, d_done, d_use2, e_len, e_trunc;
    reg [7:0]  n_lh;
    always @* begin
        n_skip = skip; n_body = body; n_lp = lp; n_lh = len_hi; n_mpos = mpos;
        n_mlen = mlen; n_disc = disc;
        d_done = 1'b0; d_use2 = 1'b0; d_len = mlen; e_len = 1'b0; e_trunc = 1'b0;
        if (in_valid && !disc) begin
            if (!lp) begin
                if (!region_ends) begin
                    n_skip = skip - {12'd0, n};
                    n_mpos = mpos + {12'd0, n};
                end else begin
                    if (body && skip != 16'd0) d_done = 1'b1;   // message ends here
                    n_skip = 16'd0; n_body = 1'b0;
                    if (p + 4'd1 == n) begin                     // split prefix
                        n_lp = 1'b1; n_lh = lane(in_data, p[2:0]);
                    end
                end
            end
            if (newbody) begin
                n_lp = 1'b0;
                if (L < MINLEN) begin
                    e_len = 1'b1; n_disc = 1'b1; n_body = 1'b0; n_skip = 16'd0;
                end else if (L <= {12'd0, avail}) begin          // only lp && L==7
                    d_done = 1'b1; d_use2 = 1'b1; d_len = L;
                    n_body = 1'b0; n_skip = 16'd0;
                end else begin
                    n_body = 1'b1; n_skip = L - {12'd0, avail};
                    n_mpos = {12'd0, avail}; n_mlen = L;
                end
            end
        end
        if (in_valid && in_last) begin
            if (!disc && !n_disc && (n_skip != 16'd0 || n_lp)) e_trunc = 1'b1;
            n_skip = HDR; n_body = 1'b0; n_lp = 1'b0; n_disc = 1'b0;
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            skip <= HDR; body <= 1'b0; lp <= 1'b0; len_hi <= 8'd0; mpos <= 16'd0;
            mlen <= 16'd0; disc <= 1'b0; cur <= {8*CAP{1'b0}}; sop <= 1'b1;
            sof_q <= 32'd0; msg_valid <= 1'b0; msg_data <= {8*CAP{1'b0}};
            msg_len <= 16'd0; msg_id <= 32'd0; msg_sof <= 32'd0; msg_eom <= 32'd0;
            cnt_msgs <= 32'd0; cnt_pkts <= 32'd0; cnt_err <= 32'd0;
        end else begin
            skip <= n_skip; body <= n_body; lp <= n_lp; len_hi <= n_lh;
            mpos <= n_mpos; mlen <= n_mlen; disc <= n_disc;
            if (in_valid && !disc) cur <= newbody ? buf2 : buf1;
            msg_valid <= d_done;
            if (d_done) begin
                msg_data <= d_use2 ? buf2 : buf1;
                msg_len  <= d_len;
                msg_id   <= cnt_msgs;
                msg_eom  <= now;
                msg_sof  <= sop ? now : sof_q;
                cnt_msgs <= cnt_msgs + 32'd1;
            end
            if (in_valid && sop) sof_q <= now;
            if (in_valid) sop <= in_last;
            if (in_valid && in_last) cnt_pkts <= cnt_pkts + 32'd1;
            if (e_len || e_trunc) cnt_err <= cnt_err + 32'd1;
        end
    end
endmodule
