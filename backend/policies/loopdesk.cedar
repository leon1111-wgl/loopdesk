// LoopDesk tool gateway: who may change what, when a person has to decide, and when the answer is no.
//
// Deny by default. A call that changes data runs only when a permit below matches and no forbid does. The backend
// evaluates this file with cedarpy and the static demo with cedar-wasm, the same Cedar engine version on both sides
// (docs/POLICY.md). Each forbid's @id says what kind of answer it is:
//   deny.*    the call is refused; the gateway tells the model or the customer why
//   review.*  the call waits in the approval queue for a person
// When several apply, the gateway shows the first in the order of this file. @stage("approval") marks the two checked
// when the application is about to be filed, after the channel gateway; the rest are checked before it.
// @locked: an invariant. The what-if panel cannot switch it off, and tests/test_policy.py checks it holds for every
// variant of this file the panel can make.

// ---------------------------------------------------------------------------- what may be called at all

@id("permit.request_refund")
permit (principal, action == LoopDesk::Action::"request_refund", resource)
when { context has refund };

@id("permit.withdraw_refund_request")
permit (principal, action == LoopDesk::Action::"withdraw_refund_request", resource)
when { principal.role == "customer" };

@id("permit.create_ticket")
permit (principal, action == LoopDesk::Action::"create_ticket", resource);

@id("permit.handoff_to_human")
permit (principal, action == LoopDesk::Action::"handoff_to_human", resource);

@id("permit.call_tenant_tool")
permit (principal, action == LoopDesk::Action::"call_tenant_tool", resource);

@id("permit.move_money")
permit (principal, action == LoopDesk::Action::"move_money", resource);

// ---------------------------------------------------------------------------- refunds: never

// Money moves only on the customer's own words.
@id("deny.customer_declined")
@label("顾客说了先不退")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { !context.reviewed && context.customer_declined };

@id("deny.amount_not_positive")
@label("退款金额必须大于 0")
@locked("true")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { context has refund && context.refund.amount <= 0 };

@id("deny.amount_above_paid")
@label("退款金额不能超过实付金额")
@locked("true")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { context has refund && context.refund.amount > context.refund.paid };

@id("deny.order_not_refundable")
@label("订单状态不能退款")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { context has refund && !context.refund.refundable_status };

@id("deny.no_online_refund")
@label("这类商品不能在线退款")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { context has refund && !context.refund.online_refund };

@id("deny.after_sales_deadline")
@label("超过售后期限，不能在线退款")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  context has refund &&
  context.refund has days_since_delivery &&
  context.refund.days_since_delivery > context.limits.last_refund_day
};

@id("deny.already_waiting")
@label("这单已有申请在等人工审核")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { !context.reviewed && context has refund && context.refund.awaiting_review };

@id("deny.not_asked")
@label("这句话不是明确的退款申请：只提议，不提交")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { !context.reviewed && !context.customer_asked };

@id("deny.item_not_on_order")
@label("消息里的商品不在这单上")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { !context.reviewed && context has refund && context.refund.item_mismatch };

@id("deny.order_unconfirmed")
@label("订单是上一轮带过来的：先确认，再提交")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { !context.reviewed && context has refund && context.refund.unconfirmed };

@id("deny.stated_amount_differs")
@label("顾客说的金额和要提交的不一致")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  !context.reviewed &&
  context has refund &&
  context.refund has stated_amount &&
  context.refund.stated_amount != context.refund.amount
};

// Velocity is counted per customer: staff filing for one need the customer's account here.
@id("deny.customer_has_no_account")
@stage("approval")
@label("顾客在本系统没有账号，员工不能代办")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  !context.reviewed &&
  principal.role != "customer" &&
  context has refund &&
  context.refund.amount <= context.limits.auto_refund_limit &&
  !context.refund.customer_has_account
};

// Four eyes, at filing time: staff file only what someone else could approve.
@id("deny.no_other_reviewer")
@stage("approval")
@label("四眼原则：没有另一位能审批的员工")
@locked("true")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  !context.reviewed &&
  principal.role != "customer" &&
  context has refund &&
  (if context.refund.amount > context.limits.approval_admin_threshold
   then !context.reviewers.other_admin
   else !context.reviewers.other_staff)
};

// ---------------------------------------------------------------------------- refunds: a person decides

@id("review.staff_filed")
@label("员工代顾客发起，需另一位员工审批")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { !context.reviewed && principal.role != "customer" };

@id("review.past_return_window")
@label("超过无理由期限，只有质量问题可以退，人工核实")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  !context.reviewed &&
  context has refund &&
  context.refund has days_since_delivery &&
  context.refund.days_since_delivery > context.limits.return_window_days
};

@id("review.above_auto_limit")
@label("超过自动审批额度")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  !context.reviewed &&
  context has refund &&
  context.refund.amount > context.limits.auto_refund_limit
};

@id("review.velocity_count")
@label("近期退款笔数超过自动审批上限")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  !context.reviewed &&
  context has refund &&
  context.refund.amount <= context.limits.auto_refund_limit &&
  context.refund.refunds_in_window + 1 > context.limits.window_max_count
};

@id("review.velocity_total")
@label("近期累计退款金额超过自动审批上限")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when {
  !context.reviewed &&
  context has refund &&
  context.refund.amount <= context.limits.auto_refund_limit &&
  context.refund.refunds_in_window + 1 <= context.limits.window_max_count &&
  context.refund.refunded_in_window + context.refund.amount > context.limits.window_max_total
};

@id("review.rejected_before")
@label("30 天内被驳回过，重新申请需人工复核")
forbid (principal, action == LoopDesk::Action::"request_refund", resource)
when { !context.reviewed && context has refund && context.refund.rejected_recently };

// ---------------------------------------------------------------------------- channels

@id("deny.voice_unconfirmed")
@label("语音渠道：资金操作要先口头确认订单号和金额")
@locked("true")
forbid (principal, action == LoopDesk::Action::"move_money", resource)
when { context.channel == "voice" && !context.reviewed && !context.spoken_confirmation };

// ---------------------------------------------------------------------------- the approval queue

@id("permit.decide")
permit (
  principal,
  action in [LoopDesk::Action::"approve", LoopDesk::Action::"reject"],
  resource
)
when { principal.role == "admin" || principal.role == "agent" };

@id("permit.withdraw_application")
permit (principal, action == LoopDesk::Action::"withdraw_application", resource)
when { resource.requester == principal };

@id("deny.four_eyes")
@label("四眼原则：不能审批自己发起的申请")
@locked("true")
forbid (
  principal,
  action in [LoopDesk::Action::"approve", LoopDesk::Action::"reject"],
  resource
)
when { resource.requester == principal };

@id("deny.approver_tier")
@label("金额超过分级线（或工具策略要求），只有管理员能批准")
forbid (principal, action == LoopDesk::Action::"approve", resource)
when {
  principal.role != "admin" &&
  (resource.approver == "admin" ||
   (resource.approver == "" && resource.amount > context.approval_admin_threshold))
};
