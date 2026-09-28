// price_levels.v -- one side of the book: DEPTH sorted price levels
// (bids descending, asks ascending), valid entries contiguous from index 0.
// One operation per cycle:
//   add(px,q): existing level -> qty += q; else insert in sorted position,
//              shifting worse levels down (the worst one falls off = "evict");
//              if the side is full and px is worse than all levels -> "drop".
//   sub(px,q): existing level -> qty -= q, or remove level if qty <= q
//              (shift better-than-removed levels stay, worse move up);
//              unknown price -> no-op (level was evicted/dropped earlier).
// q == 0 adds are no-ops.
module price_levels #(
    parameter DEPTH  = 8,
    parameter IS_BID = 1
) (
    input  wire                clk,
    input  wire                rst,
    input  wire                op_v,
    input  wire                op_add,
    input  wire [31:0]         op_px,
    input  wire [31:0]         op_q,
    output wire [DEPTH-1:0]    v_o,
    output wire [32*DEPTH-1:0] px_o,
    output wire [32*DEPTH-1:0] q_o,
    output wire                ev,
    output wire                dr
);
    reg  [DEPTH-1:0] vld;
    reg  [31:0]      px [0:DEPTH-1];
    reg  [31:0]      q  [0:DEPTH-1];
    wire [DEPTH-1:0] match;
    wire [DEPTH-1:0] better;
    wire [DEPTH-1:0] at_or_after_m;   // index >= matching index
    wire [DEPTH-1:0] ins_here;        // first "better" index
    wire [31:0]      mq_part [0:DEPTH-1];

    genvar i;
    generate
        for (i = 0; i < DEPTH; i = i + 1) begin : g_cmp
            assign match[i]  = vld[i] && (px[i] == op_px);
            assign better[i] = !vld[i] || (IS_BID ? (op_px > px[i]) : (op_px < px[i]));
            assign px_o[32*i +: 32] = px[i];
            assign q_o [32*i +: 32] = q[i];
            if (i == 0) begin : g0
                assign at_or_after_m[i] = match[i];
                assign ins_here[i]      = better[i];
                assign mq_part[i]       = match[i] ? q[i] : 32'd0;
            end else begin : gn
                assign at_or_after_m[i] = at_or_after_m[i-1] | match[i];
                assign ins_here[i]      = better[i] & ~better[i-1];
                assign mq_part[i]       = mq_part[i-1] | (match[i] ? q[i] : 32'd0);
            end
        end
    endgenerate
    assign v_o = vld;

    wire        any_match = |match;
    wire [31:0] mq        = mq_part[DEPTH-1];
    wire        ins       = op_add && !any_match && better[DEPTH-1] && (op_q != 0);
    wire        remove    = !op_add && any_match && (mq <= op_q);
    assign ev = op_v && ins && vld[DEPTH-1];
    assign dr = op_v && op_add && !any_match && !better[DEPTH-1] && (op_q != 0);

    generate
        for (i = 0; i < DEPTH; i = i + 1) begin : g_upd
            always @(posedge clk) begin
                if (rst) begin
                    vld[i] <= 1'b0;
                    px[i]  <= 32'd0;
                    q[i]   <= 32'd0;
                end else if (op_v) begin
                    if (ins) begin
                        if (ins_here[i]) begin
                            vld[i] <= 1'b1; px[i] <= op_px; q[i] <= op_q;
                        end else if (better[i]) begin
                            // i > insertion point: shift down from i-1
                            if (i > 0) begin
                                vld[i] <= vld[(i > 0) ? i-1 : 0];
                                px[i]  <= px[(i > 0) ? i-1 : 0];
                                q[i]   <= q[(i > 0) ? i-1 : 0];
                            end
                        end
                    end else if (remove) begin
                        if (at_or_after_m[i]) begin
                            if (i == DEPTH-1) begin
                                vld[i] <= 1'b0;
                            end else begin
                                vld[i] <= vld[(i < DEPTH-1) ? i+1 : i];
                                px[i]  <= px[(i < DEPTH-1) ? i+1 : i];
                                q[i]   <= q[(i < DEPTH-1) ? i+1 : i];
                            end
                        end
                    end else if (match[i]) begin
                        q[i] <= op_add ? (q[i] + op_q) : (q[i] - op_q);
                    end
                end
            end
        end
    endgenerate
endmodule
