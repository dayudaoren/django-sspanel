import datetime
import time
from decimal import Decimal
from urllib.parse import urlencode
import base64

import markdown
import pendulum
from django.conf import settings
from django.forms.models import model_to_dict
from django.contrib.auth.models import AbstractUser
from django.core.mail import send_mail
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import connection, models, transaction
from django.utils import timezone

from apps.constants import (
    NODE_TIME_OUT,
    THEME_CHOICES,
    METHOD_CHOICES,
    COUNTRIES_CHOICES,
)
from apps.encoder import encoder
from apps.payments import pay
from apps.utils import get_long_random_string, traffic_format


class User(AbstractUser):

    SUB_TYPE_SS = 0
    SUB_TYPE_SSR = 1
    SUB_TYPE_ALL = 2

    SUB_TYPES = (
        (SUB_TYPE_SS, "只订阅SS"),
        (SUB_TYPE_SSR, "只订阅SSR"),
        (SUB_TYPE_ALL, "订阅所有"),
    )

    balance = models.DecimalField(
        verbose_name="余额",
        decimal_places=2,
        max_digits=10,
        default=0,
        editable=True,
        null=True,
        blank=True,
    )
    invitecode_num = models.PositiveIntegerField(
        verbose_name="可生成的邀请码数量", default=settings.INVITE_NUM
    )
    level = models.PositiveIntegerField(
        verbose_name="用户等级",
        default=0,
        validators=[MaxValueValidator(9), MinValueValidator(0)],
    )
    level_expire_time = models.DateTimeField(verbose_name="等级有效期", default=timezone.now)
    theme = models.CharField(
        verbose_name="主题",
        choices=THEME_CHOICES,
        default=settings.DEFAULT_THEME,
        max_length=10,
    )
    # TODO Move To UserSsConfig
    sub_type = models.SmallIntegerField(
        verbose_name="订阅类型", choices=SUB_TYPES, default=SUB_TYPE_ALL
    )
    inviter_id = models.PositiveIntegerField(verbose_name="邀请人id", default=1)

    class Meta(AbstractUser.Meta):
        verbose_name = "用户"

    def delete(self):
        self.ss_user.delete()
        return super(User, self).delete()

    def __str__(self):
        return self.username

    @classmethod
    def get_total_user_num(cls):
        """返回用户总数"""
        return cls.objects.all().count()

    @classmethod
    def get_today_register_user(cls):
        """返回今日注册的用户"""
        return cls.objects.filter(date_joined__gt=pendulum.today())

    @classmethod
    @transaction.atomic
    def add_new_user(cls, cleaned_data):
        from apps.ssserver.models import Suser

        user = cls.objects.create_user(
            cleaned_data["username"], cleaned_data["email"], cleaned_data["password1"]
        )
        if "invitecode" in cleaned_data:
            code = InviteCode.objects.get(code=cleaned_data["invitecode"])
            code.consume()
            inviter_id = code.user_id
        elif "ref" in cleaned_data:
            inviter_id = encoder.string2int(cleaned_data["ref"])
        # 绑定邀请人
        UserRefLog.log_ref(inviter_id, pendulum.today())
        user.inviter_id = inviter_id
        user.save()
        # 绑定suser
        Suser.create_by_user_id(user.id)
        Suser.clear_get_user_configs_by_node_id_cache()
        return user

    @classmethod
    def get_by_user_name(cls, username):
        return cls.objects.get(username=username)

    @classmethod
    def get_by_pk(cls, pk):
        return cls.objects.get(pk=pk)

    @classmethod
    def check_and_disable_expired_users(cls):
        from apps.ssserver.models import Suser

        now = pendulum.now()
        expired_user_emails = []
        expired_users = cls.objects.filter(level__gt=0, level_expire_time__lte=now)
        for user in expired_users:
            user.ss_user.reset_to_fresh()
            user.level = 0
            user.save()
            print(f"time: {now} user: {user} level timeout!")
            expired_user_emails.append(user.email)
        if expired_users:
            Suser.clear_get_user_configs_by_node_id_cache()
        if expired_user_emails and settings.EXPIRE_EMAIL_NOTICE:
            send_mail(
                f"您的{settings.TITLE}账号已到期",
                f"您的账号现被暂停使用。如需继续使用请前往 {settings.HOST} 充值",
                settings.DEFAULT_FROM_EMAIL,
                expired_user_emails,
            )

    @property
    def sub_link(self):
        """订阅地址"""
        params = {"token": self.ss_user.token}
        return settings.HOST + f"/api/subscribe/?{urlencode(params)}"

    @property
    def ref_link(self):
        """ref地址"""
        params = {"ref": self.ss_user.token}
        return settings.HOST + f"/sspanel/register/?{urlencode(params)}"

    @property
    def ss_user(self):
        from apps.ssserver.models import Suser

        return Suser.objects.get(user_id=self.id)

    def get_sub_links(self):
        # TODO 暂时只能处理SS节点的订阅

        node_list = SSNode.get_active_nodes()
        ss_user = self.ss_user
        sub_links = "MAX={}\n".format(node_list.count())
        for node in node_list:
            sub_links += node.get_ss_link(ss_user) + "\n"
        return sub_links


class InviteCode(models.Model):
    """邀请码"""

    TYPE_PUBLIC = 1
    TYPE_PRIVATE = 0
    INVITE_CODE_TYPE = ((TYPE_PUBLIC, "公开"), (TYPE_PRIVATE, "不公开"))

    code = models.CharField(
        verbose_name="邀请码",
        primary_key=True,
        blank=True,
        max_length=40,
        default=get_long_random_string,
    )
    code_type = models.IntegerField(
        verbose_name="类型", choices=INVITE_CODE_TYPE, default=TYPE_PRIVATE
    )
    user_id = models.PositiveIntegerField(verbose_name="邀请人ID", default=1)
    used = models.BooleanField(verbose_name="是否使用", default=False)
    created_at = models.DateTimeField(editable=False, auto_now_add=True)

    def __str__(self):
        return f"<{self.user_id}>-<{self.code}>"

    class Meta:
        verbose_name_plural = "邀请码"
        ordering = ("used", "-created_at")

    @classmethod
    def calc_num_by_user(cls, user):
        return user.invitecode_num - cls.list_not_used_by_user_id(user.pk).count()

    @classmethod
    def create_by_user(cls, user):
        num = cls.calc_num_by_user(user)
        if num > 0:
            models = [cls(code_type=0, user_id=user.pk) for _ in range(num)]
            cls.objects.bulk_create(models)
        return num

    @classmethod
    def list_by_code_type(cls, code_type, num=20):
        return cls.objects.filter(code_type=code_type, used=False)[:num]

    @classmethod
    def list_by_user_id(cls, user_id, num=10):
        return cls.objects.filter(user_id=user_id)[:num]

    @classmethod
    def list_not_used_by_user_id(cls, user_id):
        return cls.objects.filter(user_id=user_id, used=False)

    def consume(self):
        self.used = True
        self.save()


class RebateRecord(models.Model):
    """返利记录"""

    user_id = models.PositiveIntegerField(verbose_name="返利人ID", default=1)
    consumer_id = models.PositiveIntegerField(
        verbose_name="消费者ID", null=True, blank=True
    )
    money = models.DecimalField(
        verbose_name="金额",
        decimal_places=2,
        null=True,
        default=0,
        max_digits=10,
        blank=True,
    )
    created_at = models.DateTimeField(editable=False, auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    @classmethod
    def list_by_user_id_with_consumer_username(cls, user_id, num=10):
        logs = cls.objects.filter(user_id=user_id)[:num]
        user_ids = [log.consumer_id for log in logs]
        username_map = {u.id: u.username for u in User.objects.filter(id__in=user_ids)}
        for log in logs:
            setattr(log, "consumer_username", username_map.get(log.consumer_id, ""))
        return logs


class Donate(models.Model):

    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="捐赠人")
    time = models.DateTimeField(
        "捐赠时间", editable=False, auto_now_add=True, db_index=True
    )
    money = models.DecimalField(
        verbose_name="捐赠金额",
        decimal_places=2,
        max_digits=10,
        default=0,
        null=True,
        blank=True,
        db_index=True,
    )

    def __str__(self):
        return "{}-{}".format(self.user, self.money)

    class Meta:
        verbose_name_plural = "捐赠记录"
        ordering = ("-time",)

    @classmethod
    def get_donate_money_by_date(cls, date=None):
        if date:
            return int(sum([d.money for d in cls.objects.filter(time__gte=date)]))
        return int(sum([d.money for d in cls.objects.all()]))

    @classmethod
    def get_donate_count_by_date(cls, date=None):
        if date:
            return cls.objects.filter(time__gte=date).count()
        return cls.objects.all().count()

    @classmethod
    def get_most_donated_user_by_count(cls, count):
        return (
            cls.objects.values("user__username")
            .annotate(amount=models.Sum("money"))
            .order_by("-amount")[:count]
        )


class MoneyCode(models.Model):
    """充值码"""

    user = models.CharField(verbose_name="用户名", max_length=128, blank=True, null=True)
    time = models.DateTimeField("捐赠时间", editable=False, auto_now_add=True)
    code = models.CharField(
        verbose_name="充值码",
        unique=True,
        blank=True,
        max_length=40,
        default=get_long_random_string,
    )
    number = models.DecimalField(
        verbose_name="捐赠金额",
        decimal_places=2,
        max_digits=10,
        default=10,
        null=True,
        blank=True,
    )
    isused = models.BooleanField(verbose_name="是否使用", default=False)

    def clean(self):
        # 保证充值码不会重复
        code_length = len(self.code or "")
        if 0 < code_length < 12:
            self.code = "{}{}".format(self.code, get_long_random_string())
        else:
            self.code = get_long_random_string()

    def __str__(self):
        return self.code

    class Meta:
        verbose_name_plural = "充值码"
        ordering = ("isused",)


class Goods(models.Model):
    """商品"""

    STATUS_TYPE = ((1, "上架"), (-1, "下架"))

    name = models.CharField(verbose_name="商品名字", max_length=128, default="待编辑")
    content = models.CharField(verbose_name="商品描述", max_length=256, default="待编辑")
    transfer = models.BigIntegerField(verbose_name="增加的流量", default=settings.GB)
    money = models.DecimalField(
        verbose_name="金额",
        decimal_places=2,
        max_digits=10,
        default=0,
        null=True,
        blank=True,
    )
    level = models.PositiveIntegerField(
        verbose_name="设置等级",
        default=0,
        validators=[MaxValueValidator(9), MinValueValidator(0)],
    )
    days = models.PositiveIntegerField(
        verbose_name="设置等级时间(天)",
        default=1,
        validators=[MaxValueValidator(365), MinValueValidator(1)],
    )
    status = models.SmallIntegerField("商品状态", default=1, choices=STATUS_TYPE)
    order = models.PositiveSmallIntegerField("排序", default=1)

    class Meta:
        verbose_name_plural = "商品"
        ordering = ["order"]

    def __str__(self):
        return self.name

    @property
    def total_transfer(self):
        """增加的流量"""
        return traffic_format(self.transfer)

    def get_days(self):
        """返回增加的天数"""
        return "{}".format(self.days)

    @transaction.atomic
    def purchase_by_user(self, user):
        """购买商品 返回是否成功"""
        if user.balance < self.money:
            return False
        # 验证成功进行提权操作
        ss_user = user.ss_user
        user.balance -= self.money
        now = pendulum.now()
        days = pendulum.duration(days=self.days)
        if user.level == self.level and user.level_expire_time > now:
            user.level_expire_time += days
            ss_user.increase_transfer(self.transfer)
        else:
            user.level_expire_time = now + days
            ss_user.reset_traffic(self.transfer)
        ss_user.enable = True
        user.level = self.level
        ss_user.save()
        user.save()
        ss_user.clear_get_user_configs_by_node_id_cache()
        # 增加购买记录
        PurchaseHistory.objects.create(
            good=self, user=user, money=self.money, purchtime=now
        )
        inviter = User.get_by_pk(user.inviter_id)
        if inviter != user:
            # 增加返利记录
            rebaterecord = RebateRecord(
                user_id=inviter.pk,
                consumer_id=user.pk,
                money=self.money * Decimal(settings.INVITE_PERCENT),
            )
            inviter.balance += rebaterecord.money
            inviter.save()
            rebaterecord.save()
        return True


class PurchaseHistory(models.Model):
    """购买记录"""

    good = models.ForeignKey(Goods, on_delete=models.CASCADE, verbose_name="商品名")
    user = models.CharField(verbose_name="购买者", max_length=128)
    money = models.DecimalField(
        verbose_name="金额",
        decimal_places=2,
        max_digits=10,
        default=0,
        null=True,
        blank=True,
    )
    purchtime = models.DateTimeField("购买时间", editable=False, auto_now_add=True)

    def __str__(self):
        return self.user

    class Meta:
        verbose_name_plural = "购买记录"
        ordering = ("-purchtime",)

    @classmethod
    def cost_statistics(cls, good_id, start, end):
        start = pendulum.parse(start, tz=timezone.get_current_timezone())
        end = pendulum.parse(end, tz=timezone.get_current_timezone())
        query = cls.objects.filter(
            good__id=good_id, purchtime__gte=start, purchtime__lte=end
        )
        for obj in query:
            print(obj.user, obj.good)
        count = query.count()
        amount = count * obj.money
        print(
            "{} ~ {} 时间内 商品: {} 共销售 {} 次 总金额 {} 元".format(
                start.date(), end.date(), obj.good, count, amount
            )
        )


class Announcement(models.Model):
    """公告界面"""

    time = models.DateTimeField("时间", auto_now_add=True)
    body = models.TextField("主体")

    def __str__(self):
        return "日期:{}".format(str(self.time)[:9])

    # 重写save函数，将文本渲染成markdown格式存入数据库
    def save(self, *args, **kwargs):
        # 首先实例化一个MarkDown类，来渲染一下body的文本 成为html文本
        md = markdown.Markdown(extensions=["markdown.extensions.extra"])
        self.body = md.convert(self.body)
        # 调动父类save 将数据保存到数据库中
        super(Announcement, self).save(*args, **kwargs)

    class Meta:
        verbose_name_plural = "系统公告"
        ordering = ("-time",)


class Ticket(models.Model):
    """工单"""

    TICKET_CHOICE = ((1, "开启"), (-1, "关闭"))
    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="用户")
    time = models.DateTimeField(verbose_name="时间", editable=False, auto_now_add=True)
    title = models.CharField(verbose_name="标题", max_length=128)
    body = models.TextField(verbose_name="内容主体")
    status = models.SmallIntegerField(
        verbose_name="状态", choices=TICKET_CHOICE, default=1
    )

    def __str__(self):
        return self.title

    class Meta:
        verbose_name_plural = "工单"
        ordering = ("-time",)


class UserOrder(models.Model):

    DEFAULT_ORDER_TIME_OUT = "24h"
    STATUS_CREATED = 0
    STATUS_PAID = 1
    STATUS_FINISHED = 2
    STATUS_CHOICES = (
        (STATUS_CREATED, "created"),
        (STATUS_PAID, "paid"),
        (STATUS_FINISHED, "finished"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="用户")
    status = models.SmallIntegerField(
        verbose_name="订单状态", db_index=True, choices=STATUS_CHOICES
    )
    out_trade_no = models.CharField(
        verbose_name="流水号", max_length=64, unique=True, db_index=True
    )
    qrcode_url = models.CharField(verbose_name="支付连接", max_length=64, null=True)
    amount = models.DecimalField(
        verbose_name="金额", decimal_places=2, max_digits=10, default=0
    )
    created_at = models.DateTimeField(
        verbose_name="时间", auto_now_add=True, db_index=True
    )
    expired_at = models.DateTimeField(verbose_name="过期时间", db_index=True)

    def __str__(self):
        return f"<{self.id,self.user}>:{self.amount}"

    class Meta:
        verbose_name_plural = "用户订单"
        index_together = ["user", "status"]

    @classmethod
    def gen_out_trade_no(cls):
        return datetime.datetime.fromtimestamp(time.time()).strftime("%Y%m%d%H%M%S%s")

    @classmethod
    def get_not_paid_order(cls, user, amount):
        return (
            cls.objects.filter(user=user, status=cls.STATUS_CREATED, amount=amount)
            .order_by("-created_at")
            .first()
        )

    @classmethod
    def get_recent_created_order(cls, user):
        return cls.objects.filter(user=user).order_by("-created_at").first()

    @classmethod
    def make_up_lost_orders(cls):
        now = pendulum.now()
        for order in cls.objects.filter(status=cls.STATUS_CREATED, expired_at__gte=now):
            changed = order.check_order_status()
            if changed:
                print(f"补单：{order.user,order.amount}")

    @classmethod
    def get_or_create_order(cls, user, amount):
        now = pendulum.now()
        order = cls.get_not_paid_order(user, amount)
        if order and order.expired_at > now:
            return order
        with transaction.atomic():
            out_trade_no = cls.gen_out_trade_no()
            trade = pay.alipay.api_alipay_trade_precreate(
                subject=settings.ALIPAY_TRADE_INFO.format(amount),
                out_trade_no=out_trade_no,
                total_amount=amount,
                timeout_express=cls.DEFAULT_ORDER_TIME_OUT,
                notify_url=settings.ALIPAY_CALLBACK_URL,
            )
            qrcode_url = trade.get("qr_code")
            order = cls.objects.create(
                user=user,
                status=cls.STATUS_CREATED,
                out_trade_no=out_trade_no,
                amount=amount,
                qrcode_url=qrcode_url,
                expired_at=now.add(hours=24),
            )
            return order

    def handle_paid(self):
        if self.status != self.STATUS_PAID:
            return
        with transaction.atomic():
            self.user.balance += self.amount
            self.user.save()
            self.status = self.STATUS_FINISHED
            self.save()
            # 将充值记录和捐赠绑定
            Donate.objects.create(user=self.user, money=self.amount)

    def check_order_status(self):
        changed = False
        if self.status != self.STATUS_CREATED:
            return
        with transaction.atomic():
            res = pay.alipay.api_alipay_trade_query(out_trade_no=self.out_trade_no)
            if res.get("trade_status", "") == "TRADE_SUCCESS":
                self.status = self.STATUS_PAID
                self.save()
                changed = True
        self.handle_paid()
        return changed


class UserRefLog(models.Model):
    user_id = models.PositiveIntegerField()
    register_count = models.IntegerField(default=0)
    date = models.DateField("记录日期", default=pendulum.today, db_index=True)

    class Meta:
        unique_together = [["user_id", "date"]]

    @classmethod
    def log_ref(cls, user_id, date):
        log, _ = cls.objects.get_or_create(user_id=user_id, date=date)
        log.register_count += 1
        log.save()

    @classmethod
    def list_by_user_id_and_date_list(cls, user_id, date_list):
        return cls.objects.filter(user_id=user_id, date__in=date_list)

    @classmethod
    def gen_bar_chart_configs(cls, user_id, date_list):
        """set register_count to 0 if the query date log not exists"""
        date_list = sorted(date_list)
        logs = {
            log.date: log.register_count
            for log in cls.list_by_user_id_and_date_list(user_id, date_list)
        }
        bar_config = {
            "labels": [f"{date.month}-{date.day}" for date in date_list],
            "data": [logs.get(date, 0) for date in date_list],
            "data_title": "每日邀请注册人数",
        }
        return bar_config


class UserOnLineIpLog(models.Model):

    user_id = models.IntegerField(db_index=True)
    node_id = models.IntegerField()
    ip = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name_plural = "用户在线IP"
        ordering = ["-created_at"]
        index_together = ["node_id", "created_at"]

    @classmethod
    def get_recent_log_by_node_id(cls, node_id):
        now = pendulum.now()
        ip_set = set()
        ret = []
        for log in cls.objects.filter(
            node_id=node_id,
            created_at__range=[now.subtract(seconds=NODE_TIME_OUT), now],
        ):
            if log.ip not in ip_set:
                ret.append(log)
            ip_set.add(log.ip)
        return ret

    @classmethod
    def truncate(cls):
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE {}".format(cls._meta.db_table))


class UserTrafficLog(models.Model):

    user_id = models.IntegerField()
    node_id = models.IntegerField()
    date = models.DateField(auto_now_add=True, db_index=True)
    upload_traffic = models.BigIntegerField("上传流量", default=0)
    download_traffic = models.BigIntegerField("下载流量", default=0)

    class Meta:
        verbose_name_plural = "流量记录"
        ordering = ["-date"]
        index_together = ["user_id", "node_id", "date"]

    @property
    def total_traffic(self):
        return traffic_format(self.download_traffic + self.upload_traffic)

    @classmethod
    def truncate(cls):
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE {}".format(cls._meta.db_table))

    @classmethod
    def calc_user_total_traffic(cls, node_id, user_id):
        logs = cls.objects.filter(user_id=user_id, node_id=node_id)
        aggs = logs.aggregate(
            u=models.Sum("upload_traffic"), d=models.Sum("download_traffic")
        )
        ut = aggs["u"] if aggs["u"] else 0
        dt = aggs["d"] if aggs["d"] else 0
        return traffic_format(ut + dt)

    @classmethod
    def calc_user_traffic_by_date(cls, user_id, node_id, date):
        logs = cls.objects.filter(node_id=node_id, user_id=user_id, date=date)
        aggs = logs.aggregate(
            u=models.Sum("upload_traffic"), d=models.Sum("download_traffic")
        )
        ut = aggs["u"] if aggs["u"] else 0
        dt = aggs["d"] if aggs["d"] else 0
        return (ut + dt) // settings.MB

    @classmethod
    def gen_line_chart_configs(cls, user_id, node_id, date_list):

        ss_node = SSNode.get_or_none_by_node_id(node_id)
        user_total_traffic = cls.calc_user_total_traffic(node_id, user_id)
        date_list = sorted(date_list)
        line_config = {
            "title": "节点 {} 当月共消耗：{}".format(ss_node.name, user_total_traffic),
            "labels": ["{}-{}".format(t.month, t.day) for t in date_list],
            "data": [
                cls.calc_user_traffic_by_date(user_id, node_id, date)
                for date in date_list
            ],
            "data_title": ss_node.name,
            "x_label": "日期 最近七天",
            "y_label": "流量 单位：MB",
        }
        return line_config


class SSNodeOnlineLog(models.Model):

    node_id = models.IntegerField()
    online_user_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name_plural = "节点在线记录"
        ordering = ["-created_at"]
        index_together = ["node_id", "created_at"]

    @property
    def is_online(self):
        return pendulum.now().subtract(seconds=NODE_TIME_OUT) < self.created_at

    @classmethod
    def truncate(cls):
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE {}".format(cls._meta.db_table))

    @classmethod
    def add_log(cls, node_id, num):
        return cls.objects.create(node_id=node_id, online_user_count=num)

    @classmethod
    def get_latest_log_by_node_id(cls, node_id):
        return cls.objects.filter(node_id=node_id).order_by("-created_at").first()

    @classmethod
    def get_all_node_online_user_count(cls):

        ss_node_ids = [node.node_id for node in SSNode.get_active_nodes()]
        count = 0
        for node_id in ss_node_ids:
            log = cls.get_latest_log_by_node_id(node_id)
            if log:
                count += log.online_user_count
        return count

    @classmethod
    def get_latest_online_log_info(cls, node_id):
        data = {"online": False, "online_user_count": 0}
        log = cls.get_latest_log_by_node_id(node_id)
        if log:
            data["online"] = log.is_online
            data["online_user_count"] = log.online_user_count
        return data


class SSNode(models.Model):

    node_id = models.IntegerField(unique=True)
    level = models.PositiveIntegerField(default=0)
    name = models.CharField("名字", max_length=32)
    info = models.CharField("节点说明", max_length=1024)
    country = models.CharField(
        "国家", default="CN", max_length=5, choices=COUNTRIES_CHOICES
    )
    server = models.CharField("服务器地址", max_length=128)
    method = models.CharField(
        "加密类型", default=settings.DEFAULT_METHOD, max_length=32, choices=METHOD_CHOICES
    )
    used_traffic = models.BigIntegerField("已用流量", default=0)
    total_traffic = models.BigIntegerField("总流量", default=settings.GB)
    enable = models.BooleanField("是否开启", default=True, db_index=True)
    custom_method = models.BooleanField("自定义加密", default=False)

    class Meta:
        verbose_name_plural = "SS节点"

    @classmethod
    def get_or_none_by_node_id(cls, node_id):
        return cls.objects.filter(node_id=node_id).first()

    @classmethod
    def get_active_nodes(cls):
        return cls.objects.filter(enable=True)

    @classmethod
    def increase_used_traffic(cls, node_id, used_traffic):
        cls.objects.filter(node_id=node_id).update(
            used_traffic=models.F("used_traffic") + used_traffic
        )

    @classmethod
    def get_user_active_nodes(cls, user):
        return cls.objects.filter(enable=True, level__lte=user.level)

    @property
    def api_endpoint(self):
        params = {"token": settings.TOKEN}
        return (
            settings.HOST + f"/api/ss_user_config/{self.node_id}/?{urlencode(params)}"
        )

    @property
    def human_total_traffic(self):
        return traffic_format(self.total_traffic)

    @property
    def human_used_traffic(self):
        return traffic_format(self.used_traffic)

    def get_ss_link(self, ss_user):
        method = ss_user.method if self.custom_method else self.method
        code = f"{method}:{ss_user.password}@{self.server}:{ss_user.port}"
        b64_code = base64.urlsafe_b64encode(code.encode()).decode()
        ss_link = "ss://{}#{}".format(b64_code, self.name)
        return ss_link

    def to_dict_with_ss_user(self, ss_user):
        data = model_to_dict(self)
        data.update(model_to_dict(ss_user))
        if not self.custom_method:
            data["method"] = self.method
        return data

    def to_dict_with_extra_info(self, ss_user):
        data = self.to_dict_with_ss_user(ss_user)
        data.update(SSNodeOnlineLog.get_latest_online_log_info(self.node_id))
        data["country"] = self.country.lower()
        data["ss_link"] = self.get_ss_link(ss_user)
        data["api_point"] = self.api_endpoint
        return data


# class UserSSConfig(models.Model):
#     # TODO migrate this

#     user_id = models.IntegerField(unique=True, db_index=True)
#     port = models.IntegerField(unique=True)
#     password = models.CharField(max_length=32, default=get_short_random_string)
#     enable = models.BooleanField(default=True)
#     speed_limit = models.IntegerField(default=0)
#     method = models.CharField(
#         default=settings.DEFAULT_METHOD, max_length=32, choices=METHOD_CHOICES
#     )
#     upload_traffic = models.BigIntegerField(verbose_name="上传流量", default=0)
#     download_traffic = models.BigIntegerField(verbose_name="下载流量", default=0)
#     transfer = models.BigIntegerField(
#         verbose_name="总流量", default=settings.DEFAULT_TRAFFIC
#     )

#     class Meta:
#         verbose_name_plural = "Shadowsocks配置"
