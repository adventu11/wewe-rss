import { HttpException, HttpStatus, Injectable, Logger } from '@nestjs/common';
import { PrismaService } from '@server/prisma/prisma.service';
import { Cron } from '@nestjs/schedule';
import { TrpcService } from '@server/trpc/trpc.service';
import { feedMimeTypeMap, feedTypes } from '@server/constants';
import { ConfigService } from '@nestjs/config';
import { Article, Feed as FeedInfo } from '@prisma/client';
import { ConfigurationType } from '@server/configuration';
import { Feed, Item } from 'feed';
import got, { Got } from 'got';
import { load } from 'cheerio';
import { minify } from 'html-minifier';
import { LRUCache } from 'lru-cache';
import pMap from '@cjs-exporter/p-map';

console.log('CRON_EXPRESSION: ', process.env.CRON_EXPRESSION);

const mpCache = new LRUCache<string, string>({
  max: 5000,
});

@Injectable()
export class FeedsService {
  private readonly logger = new Logger(this.constructor.name);

  private request: Got;
  constructor(
    private readonly prismaService: PrismaService,
    private readonly trpcService: TrpcService,
    private readonly configService: ConfigService,
  ) {
    this.request = got.extend({
      retry: {
        limit: 3,
        methods: ['GET'],
      },
      timeout: 8 * 1e3,
      headers: {
        accept:
          'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'accept-encoding': 'gzip, deflate, br',
        'accept-language': 'en-US,en;q=0.9',
        'cache-control': 'max-age=0',
        'sec-ch-ua':
          '" Not A;Brand";v="99", "Chromium";v="101", "Google Chrome";v="101"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"macOS"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent':
          'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36',
      },
      hooks: {
        beforeRetry: [
          async (options, error, retryCount) => {
            this.logger.warn(`retrying ${options.url}...`);
            return new Promise((resolve) =>
              setTimeout(resolve, 2e3 * (retryCount || 1)),
            );
          },
        ],
      },
    });
  }

  @Cron(process.env.CRON_EXPRESSION || '35 5,17 * * *', {
    name: 'updateFeeds',
    timeZone: 'Asia/Shanghai',
  })
  async handleUpdateFeedsCron() {
    this.logger.debug('Called handleUpdateFeedsCron');

    // 优化：拉取定时同步任务时，只拉取必要字段，不再 SELECT *
    const feeds = await this.prismaService.feed.findMany({
      where: { status: 1 },
      select: { id: true }
    });
    this.logger.debug('feeds length:' + feeds.length);

    const updateDelayTime =
      this.configService.get<ConfigurationType['feed']>(
        'feed',
      )!.updateDelayTime;

    for (const feed of feeds) {
      this.logger.debug('feed', feed.id);
      try {
        await this.trpcService.refreshMpArticlesAndUpdateFeed(feed.id);

        await new Promise((resolve) =>
          setTimeout(resolve, updateDelayTime * 1e3),
        );
      } catch (err) {
         BronzeLogger: this.logger.error('handleUpdateFeedsCron error', err);
      } finally {
        // wait 30s for next feed
        await new Promise((resolve) => setTimeout(resolve, 30 * 1e3));
      }
    }
  }

  async cleanHtml(source: string) {
    const $ = load(source, { decodeEntities: false });

    const dirtyHtml = $.html($('.rich_media_content'));

    if (!dirtyHtml) return ''; // 安全防线：如果抓取失败或者没有内容，防止后续 replace 报错

    const html = dirtyHtml
      .replace(/data-src=/g, 'src=')
      .replace(/opacity: 0( !important)?;/g, '')
      .replace(/visibility: hidden;/g, '');

    const content =
      '<style> .rich_media_content {overflow: hidden;color: #222;font-size: 17px;word-wrap: break-word;-webkit-hyphens: auto;-ms-hyphens: auto;hyphens: auto;text-align: justify;position: relative;z-index: 0;}.rich_media_content {font-size: 18px;}</style>' +
      html;

    const result = minify(content, {
      removeAttributeQuotes: true,
      collapseWhitespace: true,
    });

    return result;
  }

  async getHtmlByUrl(url: string) {
    const html = await this.request(url, { responseType: 'text' }).text();
    if (
      this.configService.get<ConfigurationType['feed']>('feed')!.enableCleanHtml
    ) {
      const result = await this.cleanHtml(html);
      return result;
    }

    return html;
  }

  async tryGetContent(id: string) {
    let content = mpCache.get(id);
    if (content) {
      return content;
    }
    const url = `https://mp.weixin.qq.com/s/${id}`;
    content = await this.getHtmlByUrl(url).catch((e) => {
      this.logger.error(`getHtmlByUrl(${url}) error: ${e.message}`);

      return '获取全文失败，请重试~';
    });
    mpCache.set(id, content);
    return content;
  }

  async renderFeed({
    type,
    feedInfo,
    articles,
    mode,
  }: {
    type: string;
    feedInfo: FeedInfo;
    articles: Article[];
    mode?: string;
  }) {
    const { originUrl, mode: globalMode } =
      this.configService.get<ConfigurationType['feed']>('feed')!;

    const link = `${originUrl}/feeds/${feedInfo.id}.${type}`;

    const feed = new Feed({
      title: feedInfo.mpName,
      description: feedInfo.mpIntro,
      id: link,
      link: link,
      language: 'zh-cn',
      image: feedInfo.mpCover,
      favicon: feedInfo.mpCover,
      copyright: '',
      updated: new Date(feedInfo.updateTime * 1e3),
      generator: 'WeWe-RSS',
      author: { name: feedInfo.mpName },
    });

    feed.addExtension({
      name: 'generator',
      objects: `WeWe-RSS`,
    });

    // 🚀 【核心优化一】避免盲目全量捞取所有 feed 字典。
    // 如果是单个公众号，直接用它的名字；如果是 'all' 聚合，我们只提取当前文章列表中涉及到的 mpId，精准查询。
    let feedMap = new Map<string, string>();
    if (feedInfo.id === 'all' && articles.length > 0) {
      const uniqueMpIds = Array.from(new Set(articles.map(a => a.mpId).filter(Boolean)));
      const relatedFeeds = await this.prismaService.feed.findMany({
        where: { id: { in: uniqueMpIds } },
        select: { id: true, mpName: true },
      });
      relatedFeeds.forEach(f => feedMap.set(f.id, f.mpName));
    }

    /**mode 高于 globalMode。如果 mode 值存在，取 mode 值*/
    const enableFullText =
      typeof mode === 'string'
        ? mode === 'fulltext'
        : globalMode === 'fulltext';

    const showAuthor = feedInfo.id === 'all';

    const mapper = async (item) => {
      const { title, id, publishTime, picUrl, mpId } = item;
      const link = `https://mp.weixin.qq.com/s/${id}`;

      // 🚀 【核心优化二】从预筛选的 Map 中直接获取公众号名称，速度极快且极省内存
      const mpName = showAuthor ? (feedMap.get(mpId) || feedInfo.mpName || '-') : '-';
      const published = new Date(publishTime * 1e3);

      let content = '';
      if (enableFullText) {
        content = await this.tryGetContent(id);
      }

      feed.addItem({
        id,
        title,
        link: link,
        guid: link,
        content,
        date: published,
        image: picUrl,
        author: showAuthor ? [{ name: mpName }] : undefined,
      });
    };

    // 🚀 【核心优化三】由于 Railway 资源紧张，将 Fulltext 网页并发处理处理数（concurrency）限制为 1 或者是 2（原代码为 2）
    // 这能让 JS 引擎有喘息机会去进行垃圾回收，防止瞬间产生大量大字符串撑爆堆内存
    await pMap(articles, mapper, { concurrency: 1, stopOnError: false });

    return feed;
  }

  async handleGenerateFeed({
    id,
    type,
    limit,
    page,
    mode,
    title_include,
    title_exclude,
  }: {
    id?: string;
    type: string;
    limit: number;
    page: number;
    mode?: string;
    title_include?: string;
    title_exclude?: string;
  }) {
    if (!feedTypes.includes(type as any)) {
      type = 'atom';
    }

    let articles: Article[];
    let feedInfo: FeedInfo;
    if (id) {
      feedInfo = (await this.prismaService.feed.findFirst({
        where: { id },
      }))!;

      if (!feedInfo) {
        throw new HttpException('不存在该feed！', HttpStatus.BAD_REQUEST);
      }

      articles = await this.prismaService.article.findMany({
        where: { mpId: id },
        orderBy: { publishTime: 'desc' },
        take: limit,
        skip: (page - 1) * limit,
      });
    } else {
      articles = await this.prismaService.article.findMany({
        orderBy: { publishTime: 'desc' },
        take: limit,
        skip: (page - 1) * limit,
      });

      const { originUrl } =
        this.configService.get<ConfigurationType['feed']>('feed')!;
      feedInfo = {
        id: 'all',
        mpName: 'WeWe-RSS All',
        mpIntro: 'WeWe-RSS 全部文章',
        mpCover: originUrl
          ? `${originUrl}/favicon.ico`
          : 'https://r2-assets.111965.xyz/wewe-rss.png',
        status: 1,
        syncTime: 0,
        updateTime: Math.floor(Date.now() / 1e3),
        hasHistory: -1,
        createdAt: new Date(),
        updated: new Date(),
      };
    }

    this.logger.log('handleGenerateFeed articles: ' + articles.length);
    const feed = await this.renderFeed({ feedInfo, articles, type, mode });

    if (title_include) {
      const includes = title_include.split('|');
      feed.items = feed.items.filter((i: Item) =>
        includes.some((k) => i.title.includes(k)),
      );
    }
    if (title_exclude) {
      const excludes = title_exclude.split('|');
      feed.items = feed.items.filter(
        (i: Item) => !excludes.some((k) => i.title.includes(k)),
      );
    }

    switch (type) {
      case 'rss':
        return { content: feed.rss2(), mimeType: feedMimeTypeMap[type] };
      case 'json':
        return { content: feed.json1(), mimeType: feedMimeTypeMap[type] };
      case 'atom':
      default:
        return { content: feed.atom1(), mimeType: feedMimeTypeMap[type] };
    }
  }

  async getFeedList() {
    const data = await this.prismaService.feed.findMany({
      select: {
        id: true,
        mpName: true,
        mpIntro: true,
        mpCover: true,
        syncTime: true,
        updateTime: true
      }
    });

    return data.map((item) => {
      return {
        id: item.id,
        name: item.mpName,
        intro: item.mpIntro,
        cover: item.mpCover,
        syncTime: item.syncTime,
        updateTime: item.updateTime,
      };
    });
  }

  async updateFeed(id: string) {
    try {
      await this.trpcService.refreshMpArticlesAndUpdateFeed(id);
    } catch (err) {
      this.logger.error('updateFeed error', err);
    } finally {
      // wait 30s for next feed
      await new Promise((resolve) => setTimeout(resolve, 30 * 1e3));
    }
  }
}
